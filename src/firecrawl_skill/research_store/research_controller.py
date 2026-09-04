"""Canonical deterministic application controller for research workflow issue #310."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from firecrawl_skill.research_domain import serialize_model

from .assessment.coverage import CoverageService
from .asset_promotion_models import AssetPromotionError
from .candidate_budget_outcomes import (
    CandidateBudgetAdmissionContext,
    CandidateBudgetHardRejected,
    CandidateBudgetOverrideRequired,
)
from .completion_provenance import (
    CompletionProvenanceError,
    load_authoritative_completion_provenance,
)
from .handoff import HandoffBuilder
from .invocation_service import InvocationError, InvocationRecord, InvocationService
from .operator_action_service import (
    ACTION_BUDGET,
    ACTION_CURATION,
    OperatorActionError,
    OperatorActionService,
)
from .orchestrator import OrchestratorConfig, OrchestratorResult
from .research_controller_contract import (
    CONTROLLER_POLICY_SCHEMA_VERSION,
    DELIVERY_HOST_HANDOFF,
    DELIVERY_SELF_SYNTHESIZED,
    DIRECTIVE_SCHEMA_VERSION,
    DISPOSITION_BLOCKED,
    DISPOSITION_CONTINUE,
    DISPOSITION_FAILED,
    DISPOSITION_OPERATOR,
    HANDOFF_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    ControllerBlockedError,
    ControllerBoundError,
    ControllerConfig,
    ProgressGuard,
    ResearchResult,
    WorkflowDirective,
    bounded_messages,
    bounded_text,
    terminal_disposition,
    validate_delivery_mode,
    validate_public_run_id,
)
from .retained_completion_service import RetainedCompletionPromotionService
from .retained_review_service import RetainedEvaluation, RetainedReviewService
from .run_service import (
    PERMITTED_TRANSITIONS,
    TERMINAL_STATES,
    ResearchRunService,
    RunStateError,
    RunStatus,
    StaleRunRevisionError,
)
from .semantic_service import SemanticCallService
from .smart_objective_intent import (
    interpret_smart_objective,
    materialize_smart_objective_intent,
)
from .smart_orchestrator import (
    PlanningBundle,
    SmartResumeError,
    load_planning_bundle,
)
from .smart_search_application import (
    QueryPlanner,
    deterministic_queries,
    initialize_planning_bundle,
)

_CONTROLLER_POLICY_EVENT = "controller.policy_recorded"
_PLANNING_OPERATION = "fresearch_planning"
_PLANNING_INPUT_SCHEMA = "fresearch-planning-input-v2"
_PLANNING_OUTPUT_SCHEMA = "fresearch-planning-result-v1"
_MAX_EVENT_READ = 2


@dataclass(frozen=True)
class ControllerPolicy:
    retained_only: bool
    evaluated_at: datetime
    curated: bool = False
    delivery_mode: str = DELIVERY_HOST_HANDOFF


def default_query_planner(
    topic: str,
    _max_queries: int,
    _semantic_service: SemanticCallService,
    _semantic_context: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Temporary issue-310 planner adapter; issue #311 owns planner policy."""
    return deterministic_queries(topic)


class ResearchWorkflowController:
    """Advance one PostgreSQL-authoritative run without outer-agent choreography."""

    def __init__(
        self,
        *,
        config: Any,
        run_service: ResearchRunService,
        invocation_service: InvocationService,
        corpus_service: Any,
        coverage_service: CoverageService,
        evidence_service: Any,
        semantic_service: SemanticCallService,
        orchestrator_factory: Callable[[OrchestratorConfig], Any],
        query_planner: QueryPlanner | None = None,
        controller_config: ControllerConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.run_service = run_service
        self.invocation_service = invocation_service
        self.corpus_service = corpus_service
        self.coverage_service = coverage_service
        self.evidence_service = evidence_service
        self.semantic_service = semantic_service
        self.orchestrator_factory = orchestrator_factory
        self.query_planner = query_planner or default_query_planner
        self.controller_config = controller_config or ControllerConfig()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.retained_review = RetainedReviewService(
            config=config,
            run_service=run_service,
            corpus_service=corpus_service,
            coverage_service=coverage_service,
            evidence_service=evidence_service,
            semantic_service=semantic_service,
            controller_config=self.controller_config,
        )
        self.retained_completion = RetainedCompletionPromotionService(
            run_service.uow_factory
        )
        self.operator_actions = OperatorActionService(run_service.uow_factory)

    def run(
        self,
        objective: str,
        *,
        retained_only: bool = False,
        curated: bool = False,
        execution_mode: str = "autonomous_local",
        delivery_mode: str = DELIVERY_HOST_HANDOFF,
    ) -> WorkflowDirective | ResearchResult:
        objective = " ".join(objective.split())
        if not objective:
            raise ValueError("research objective is required")
        evaluated_at = self.clock()
        if evaluated_at.tzinfo is None:
            raise ValueError("controller clock must be timezone-aware")
        policy = ControllerPolicy(
            retained_only=bool(retained_only),
            curated=bool(curated),
            evaluated_at=evaluated_at.astimezone(timezone.utc),
            delivery_mode=validate_delivery_mode(delivery_mode),
        )
        external_id = f"fr_{uuid4().hex}"
        status = self.run_service.create(
            objective,
            external_id,
            execution_mode=execution_mode,
            actor_type="controller",
            actor_identifier="ResearchWorkflowController",
            metadata={
                "controller": "research-controller-v1",
                "retained_only": bool(retained_only),
                "curated": bool(curated),
                "run_mode": "curated" if curated else "autonomous",
                "delivery_mode": policy.delivery_mode,
            },
        )
        self._record_policy(status, policy)
        return self.continue_run(external_id)

    def continue_run(self, external_id: str) -> WorkflowDirective | ResearchResult:
        external_id = validate_public_run_id(external_id)
        status = self.run_service.status(external_id=external_id)

        try:
            policy = self._load_policy(status)
            guard = ProgressGuard(self.controller_config)
            while status.state not in TERMINAL_STATES:
                guard.observe(status)
                active_action = self.operator_actions.active_for_run(status)
                if active_action is not None:
                    return self._directive(
                        status,
                        DISPOSITION_OPERATOR,
                        action_kind=active_action.kind,
                        action_id=active_action.action_id,
                        diagnostics=[
                            "a genuine human authorization boundary was reached"
                        ],
                    )
                bundle = load_planning_bundle(self.run_service, status.id)
                if bundle is not None:
                    self._tighten_guard_to_budget(guard, bundle)
                    self._reconcile_planning_invocation(
                        status,
                        policy,
                        bundle,
                        allow_running=status.state == "created",
                    )

                if status.state in {"created", "planning"}:
                    if bundle is None:
                        bundle = self._initialize_planning(status, policy)
                        self._tighten_guard_to_budget(guard, bundle)
                    status = self._advance_planning(status)
                    continue

                if bundle is None:
                    raise ControllerBlockedError(
                        "run in "
                        f"{status.state} has no complete persisted planning tuple"
                    )

                if status.state == "corpus_review":
                    status = self._enter_retained_review(status, bundle)
                    continue

                if status.state == "retrieving":
                    if policy.curated and not self.operator_actions.curation_completed(
                        status
                    ):
                        selection = self.retained_review.ensure_selection(
                            status, bundle
                        )
                        if selection:
                            action = self.operator_actions.ensure_curation_action(
                                status
                            )
                            return self._directive(
                                status,
                                DISPOSITION_OPERATOR,
                                action_kind=ACTION_CURATION,
                                action_id=action.action_id,
                                diagnostics=[
                                    "curated mode requires one authoritative evidence selection"
                                ],
                            )
                    evaluation = (
                        self.retained_review.evaluate_curated(
                            status,
                            bundle,
                            evaluated_at=policy.evaluated_at,
                        )
                        if policy.curated
                        else self.retained_review.evaluate(
                            status,
                            bundle,
                            evaluated_at=policy.evaluated_at,
                        )
                    )
                    response = self._apply_retained_decision(
                        status,
                        evaluation,
                        policy,
                    )
                    if isinstance(response, WorkflowDirective):
                        return response
                    status = response
                    continue

                if status.state == "coverage_review":
                    evaluation = self.retained_review.load_evaluation(status.id)
                    if (
                        evaluation is not None
                        and self._acquisition_wave_count(status.id) == 0
                    ):
                        response = self._apply_retained_decision(
                            status,
                            evaluation,
                            policy,
                        )
                        if isinstance(response, WorkflowDirective):
                            return response
                        status = response
                        continue

                if policy.retained_only and status.state in {
                    "acquiring",
                    "extracting",
                    "indexing",
                }:
                    status = self._close_retained_only_escape(status)
                    continue

                stop_for_curation = (
                    policy.curated
                    and not self.operator_actions.curation_completed(status)
                )
                result = self._resume_existing_orchestrator(
                    status,
                    bundle,
                    delivery_mode=policy.delivery_mode,
                    stop_after_indexing=stop_for_curation,
                )
                latest = self.run_service.status(external_id=external_id)
                if (
                    policy.curated
                    and latest.state == "indexing"
                    and not self.operator_actions.curation_completed(latest)
                ):
                    action = self.operator_actions.ensure_curation_action(latest)
                    return self._directive(
                        latest,
                        DISPOSITION_OPERATOR,
                        action_kind=ACTION_CURATION,
                        action_id=action.action_id,
                        diagnostics=[
                            "curated mode requires one authoritative evidence selection"
                        ],
                    )
                return self._response_from_orchestrator(external_id, result)

            return self.result(external_id)
        except ControllerBoundError as exc:
            latest = self.run_service.status(external_id=external_id)
            latest = self._fail_closed(latest, str(exc))
            if latest.state in TERMINAL_STATES:
                return self.result(external_id)
            return self._directive(
                latest,
                DISPOSITION_BLOCKED,
                action_kind="inspect_blocker",
                diagnostics=[str(exc)],
            )
        except (
            ControllerBlockedError,
            OperatorActionError,
            RunStateError,
            SmartResumeError,
            StaleRunRevisionError,
        ) as exc:
            latest = self.run_service.status(external_id=external_id)
            return self._directive(
                latest,
                DISPOSITION_BLOCKED,
                action_kind="inspect_blocker",
                diagnostics=[str(exc)],
            )

    def status(self, external_id: str) -> WorkflowDirective:
        external_id = validate_public_run_id(external_id)
        status = self.run_service.status(external_id=external_id)
        ready = self._handoff_ready(status)
        if status.state in TERMINAL_STATES:
            if status.state == "completed" and not ready:
                return self._directive(
                    status,
                    DISPOSITION_BLOCKED,
                    action_kind="inspect_blocker",
                    handoff_ready=False,
                    diagnostics=[
                        status.error,
                        "completed lifecycle has no verifiable canonical handoff",
                    ],
                )
            return self._directive(
                status,
                terminal_disposition(status.state),
                handoff_ready=ready,
                diagnostics=[status.error] if status.error else [],
            )

        try:
            # A nonterminal workflow directive is machine authority. It may not
            # authorize continuation or operator choreography unless the run has
            # the same canonical controller policy required by continue_run().
            self._load_policy(status)

            operator_action = self.operator_actions.active_for_run(status)
            if operator_action is not None:
                return self._directive(
                    status,
                    DISPOSITION_OPERATOR,
                    action_kind=operator_action.kind,
                    action_id=operator_action.action_id,
                    diagnostics=["a genuine human authorization boundary was reached"],
                )

            evaluation = self.retained_review.load_evaluation(status.id)
            if evaluation is not None and evaluation.outcome == "blocked":
                return self._directive(
                    status,
                    DISPOSITION_BLOCKED,
                    action_kind="inspect_blocker",
                    diagnostics=[evaluation.reason],
                )
        except (ControllerBlockedError, OperatorActionError) as exc:
            return self._directive(
                status,
                DISPOSITION_BLOCKED,
                action_kind="inspect_blocker",
                diagnostics=[str(exc)],
            )
        return self._directive(
            status,
            DISPOSITION_CONTINUE,
            action_kind="continue",
            handoff_ready=ready,
        )

    def result(self, external_id: str) -> ResearchResult:
        external_id = validate_public_run_id(external_id)
        status = self.run_service.status(external_id=external_id)
        terminal = status.state in TERMINAL_STATES
        directive = self.status(external_id)
        diagnostics: list[Any] = [status.error] if status.error else []
        limitations: list[Any] = []
        if status.state == "partial":
            limitations.append(
                "terminal partial result does not establish objective satisfaction"
            )
        if not terminal:
            limitations.append("run is nonterminal; continue the same public run")
        delivery_mode: str | None = None
        handoff: dict[str, Any] | None = None
        delivery_blocked = False
        if status.state in {"completed", "partial"}:
            try:
                policy = self._load_policy(status)
                delivery_mode = policy.delivery_mode
                handoff = self._build_public_handoff(status, policy.delivery_mode)
            except (
                CompletionProvenanceError,
                ControllerBlockedError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                message = f"authoritative handoff unavailable: {bounded_text(exc)}"
                if status.state == "completed":
                    # A completed lifecycle cannot be presented as a usable final
                    # result when its canonical handoff no longer verifies. Keep
                    # the persisted lifecycle fact visible, but fail the delivery
                    # contract closed instead of silently downgrading authority.
                    delivery_blocked = True
                    diagnostics.append(message)
                else:
                    limitations.append(message)
        handoff_ready = handoff is not None
        return ResearchResult(
            schema_version=RESULT_SCHEMA_VERSION,
            run_id=external_id,
            objective=status.objective,
            lifecycle_state=status.state,
            lifecycle_revision=status.lifecycle_revision,
            disposition=(
                DISPOSITION_BLOCKED if delivery_blocked else directive.disposition
            ),
            terminal=terminal,
            outcome=status.declared_outcome,
            result_ready=terminal and not delivery_blocked,
            handoff_ready=handoff_ready,
            objective_satisfied=status.state == "completed",
            delivery_mode=delivery_mode,
            handoff=handoff,
            action_kind=directive.action_kind,
            action_id=directive.action_id,
            diagnostics=bounded_messages(diagnostics),
            limitations=bounded_messages(limitations),
        )

    def action(self, action_id: str) -> dict[str, Any]:
        """Return one sanitized public operator-action record."""
        return self.operator_actions.describe(action_id).to_public_dict()

    def approve(
        self,
        action_id: str,
        *,
        reason: str,
        authorized_by: str,
    ) -> WorkflowDirective | ResearchResult:
        action = self.operator_actions.approve(
            action_id,
            reason=reason,
            authorized_by=authorized_by,
        )
        return self.continue_run(action.public_run_id)

    def curate(
        self,
        action_id: str,
        *,
        retain_subject_ids: list[UUID],
        reject_rest: bool,
        reason: str,
        authorized_by: str,
    ) -> WorkflowDirective | ResearchResult:
        action = self.operator_actions.curate(
            action_id,
            retain_subject_ids=retain_subject_ids,
            reject_rest=reject_rest,
            reason=reason,
            authorized_by=authorized_by,
        )
        return self.continue_run(action.public_run_id)

    def fork(
        self,
        action_id: str,
        revised_objective: str,
        *,
        reason: str,
        authorized_by: str,
    ) -> WorkflowDirective | ResearchResult:
        _action, child_run_id = self.operator_actions.fork(
            action_id,
            revised_objective,
            reason=reason,
            authorized_by=authorized_by,
        )
        return self.continue_run(child_run_id)

    @staticmethod
    def _planning_external_invocation_id(run_id: UUID) -> str:
        identity = uuid5(NAMESPACE_URL, f"fresearch-planning:{run_id}")
        return f"fc_{identity.hex}"

    @staticmethod
    def _planning_invocation_input(
        status: RunStatus,
        policy: ControllerPolicy,
    ) -> dict[str, Any]:
        execution_mode = str(
            getattr(status.execution_mode, "value", status.execution_mode)
        )
        return {
            "schema_version": _PLANNING_INPUT_SCHEMA,
            "objective": status.objective,
            "execution_mode": execution_mode,
            "curated": policy.curated,
            "evaluated_at": policy.evaluated_at.isoformat(),
        }

    @staticmethod
    def _planning_invocation_output(bundle: PlanningBundle) -> dict[str, Any]:
        return {
            "schema_version": _PLANNING_OUTPUT_SCHEMA,
            "research_spec_row_id": str(bundle.spec_row_id),
            "research_spec_revision": bundle.spec_revision,
            "budget_row_id": str(bundle.budget_row_id),
            "search_plan_row_id": str(bundle.plan_row_id),
            "search_plan_revision": bundle.plan_revision,
        }

    def _begin_planning_invocation(
        self,
        status: RunStatus,
        policy: ControllerPolicy,
    ) -> InvocationRecord:
        external_invocation_id = self._planning_external_invocation_id(status.id)
        planning_input = self._planning_invocation_input(status, policy)
        try:
            invocation = self.invocation_service.begin(
                status.id,
                external_invocation_id,
                _PLANNING_OPERATION,
                planning_input,
                idempotency_key=f"controller:planning-invocation:{status.id}",
                actor_type="controller",
            )
        except (InvocationError, KeyError, TypeError, ValueError) as exc:
            raise ControllerBlockedError(
                "authoritative planning invocation could not begin: "
                f"{bounded_text(exc)}"
            ) from exc
        if invocation.external_invocation_id != external_invocation_id:
            raise ControllerBlockedError(
                "authoritative planning invocation returned contradictory external identity"
            )
        return invocation

    def _persist_planning(
        self,
        status: RunStatus,
        policy: ControllerPolicy,
        invocation: InvocationRecord,
    ) -> PlanningBundle:
        interpreted = interpret_smart_objective(
            semantic_service=self.semantic_service,
            status=status,
            objective=status.objective,
            invocation_id=str(invocation.id),
            evaluated_at=policy.evaluated_at,
        )
        if interpreted.error or not interpreted.value:
            raise ControllerBlockedError(
                "semantic objective interpretation failed: "
                f"{bounded_text(interpreted.error or 'empty structured artifact')}"
            )
        materialized = materialize_smart_objective_intent(
            interpreted.value,
            execution_mode=status.execution_mode,
            evaluated_at=policy.evaluated_at,
        )
        provenance = {
            **dict(interpreted.provenance),
            "semantic_call_id": (
                str(interpreted.semantic_call_id)
                if interpreted.semantic_call_id is not None
                else None
            ),
            "artifact_ids": [str(value) for value in interpreted.artifact_ids],
        }
        external_invocation_id = invocation.external_invocation_id
        if not external_invocation_id:
            raise ControllerBlockedError(
                "authoritative planning invocation has no external identity"
            )
        return initialize_planning_bundle(
            self.run_service,
            status,
            topic=status.objective,
            spec=materialized.spec,
            invocation_id=external_invocation_id,
            planner=self.query_planner,
            candidate_budget=self.retained_completion.candidate_budget,
            discovery_window=materialized.discovery_window,
            objective_intent_provenance=provenance,
        )

    def _complete_planning_invocation(
        self,
        status: RunStatus,
        invocation: InvocationRecord,
        bundle: PlanningBundle,
    ) -> InvocationRecord:
        try:
            return self.invocation_service.complete(
                status.id,
                invocation.id,
                "succeeded",
                output=self._planning_invocation_output(bundle),
                actor_type="controller",
            )
        except (InvocationError, KeyError, TypeError, ValueError) as exc:
            try:
                latest = self.invocation_service.status(invocation_id=invocation.id)
            except (KeyError, TypeError, ValueError) as status_exc:
                raise ControllerBlockedError(
                    "authoritative planning invocation completion could not be verified"
                ) from status_exc
            if latest.run_id == status.id and latest.status == "complete":
                return latest
            raise ControllerBlockedError(
                "authoritative planning invocation could not complete: "
                f"{bounded_text(exc)}"
            ) from exc

    def _fail_planning_invocation(
        self,
        status: RunStatus,
        invocation: InvocationRecord,
        error: Exception,
    ) -> None:
        try:
            latest = self.invocation_service.status(invocation_id=invocation.id)
        except (KeyError, TypeError, ValueError) as exc:
            raise ControllerBlockedError(
                "failed planning invocation could not be re-read authoritatively"
            ) from exc
        if latest.run_id != status.id:
            raise ControllerBlockedError(
                "failed planning invocation belongs to another research run"
            )
        if latest.status == "failed":
            return
        if latest.status != "running":
            raise ControllerBlockedError(
                "planning failed after its authoritative invocation became terminal "
                f"with contradictory status {latest.status}"
            )
        try:
            self.invocation_service.complete(
                status.id,
                latest.id,
                "failed",
                output={"schema_version": _PLANNING_OUTPUT_SCHEMA},
                error=bounded_text(error),
                actor_type="controller",
            )
        except (InvocationError, KeyError, TypeError, ValueError) as exc:
            raise ControllerBlockedError(
                "failed planning invocation could not be terminalized authoritatively"
            ) from exc

    def _initialize_planning(
        self,
        status: RunStatus,
        policy: ControllerPolicy,
    ) -> PlanningBundle:
        invocation = self._begin_planning_invocation(status, policy)
        try:
            bundle = self._persist_planning(status, policy, invocation)
        except Exception as exc:
            self._fail_planning_invocation(status, invocation, exc)
            if isinstance(exc, ControllerBlockedError):
                raise
            raise ControllerBlockedError(
                f"controller planning failed: {bounded_text(exc)}"
            ) from exc
        self._complete_planning_invocation(status, invocation, bundle)
        return bundle

    def _reconcile_planning_invocation(
        self,
        status: RunStatus,
        policy: ControllerPolicy,
        bundle: PlanningBundle,
        *,
        allow_running: bool,
    ) -> InvocationRecord:
        external_invocation_id = self._planning_external_invocation_id(status.id)
        expected_input = self._planning_invocation_input(status, policy)
        try:
            invocation = self.invocation_service.status(
                external_invocation_id=external_invocation_id
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ControllerBlockedError(
                "persisted planning tuple has no authoritative planning invocation"
            ) from exc
        if invocation.run_id != status.id:
            raise ControllerBlockedError(
                "persisted planning invocation belongs to another research run"
            )
        if invocation.operation != _PLANNING_OPERATION:
            raise ControllerBlockedError(
                "persisted planning invocation has a contradictory operation"
            )
        if invocation.external_invocation_id != external_invocation_id:
            raise ControllerBlockedError(
                "persisted planning invocation has a contradictory external identity"
            )
        if invocation.input != expected_input:
            raise ControllerBlockedError(
                "persisted planning invocation input contradicts controller policy"
            )
        expected_output = self._planning_invocation_output(bundle)
        if invocation.status == "complete":
            if invocation.output != expected_output:
                raise ControllerBlockedError(
                    "completed planning invocation contradicts the persisted planning tuple"
                )
            return invocation
        if invocation.status == "running":
            if not allow_running:
                raise ControllerBlockedError(
                    "planning invocation is still running after the planning lifecycle phase"
                )
            return self._complete_planning_invocation(status, invocation, bundle)
        if invocation.status == "failed":
            raise ControllerBlockedError(
                "persisted planning tuple is paired with a failed planning invocation"
            )
        raise ControllerBlockedError(
            "persisted planning invocation has unsupported status "
            f"{invocation.status!r}"
        )

    def _advance_planning(self, status: RunStatus) -> RunStatus:
        if status.state == "created":
            return self._transition(
                status,
                "planning",
                key=f"controller:planning:{status.id}",
                reason="controller resolved a complete planning tuple",
            )
        if status.state == "planning":
            return self._transition(
                status,
                "corpus_review",
                key=f"controller:corpus-review:{status.id}",
                reason="controller planning complete",
            )
        return status

    def _enter_retained_review(
        self,
        status: RunStatus,
        bundle: PlanningBundle,
    ) -> RunStatus:
        if not self.coverage_service.coverage_items_exist(status.id):
            self.coverage_service.create_items_from_spec(
                status.id,
                serialize_model(bundle.spec),
                execution_mode=status.execution_mode,
                idempotency_key=(
                    f"controller:coverage:{status.id}:spec{bundle.spec_revision}"
                ),
            )
        return self._transition(
            self.run_service.status(run_id=status.id),
            "retrieving",
            key=f"controller:retained-review:{status.id}",
            reason="evaluate authoritative retained corpus before provider acquisition",
        )

    def _apply_retained_decision(
        self,
        status: RunStatus,
        evaluation: RetainedEvaluation,
        policy: ControllerPolicy,
    ) -> RunStatus | WorkflowDirective:
        if evaluation.outcome == "blocked":
            return self._directive(
                status,
                DISPOSITION_BLOCKED,
                action_kind="inspect_blocker",
                diagnostics=[evaluation.reason],
            )

        if status.state == "retrieving":
            status = self._transition(
                status,
                "coverage_review",
                key=f"controller:retained-reviewed:{status.id}",
                reason=evaluation.reason or "retained review complete",
            )

        if evaluation.outcome == "sufficient":
            if status.state == "coverage_review":
                boundary = self._prepare_retained_completion_membership(status)
                if boundary is not None:
                    return boundary
                return self._transition(
                    status,
                    "synthesizing",
                    key=f"controller:retained-sufficient:{status.id}",
                    reason="retained evidence satisfied authoritative coverage",
                )
            return status

        if evaluation.outcome != "insufficient":
            raise ControllerBlockedError(
                "unsupported persisted retained evaluation outcome: "
                f"{evaluation.outcome!r}"
            )

        if policy.retained_only:
            return self._terminalize_retained_only(status)

        if status.state == "coverage_review":
            return self._transition(
                status,
                "acquiring",
                key=f"controller:retained-insufficient:{status.id}",
                reason="retained evidence insufficient; bounded acquisition required",
            )
        return status

    def _prepare_retained_completion_membership(
        self,
        status: RunStatus,
    ) -> WorkflowDirective | None:
        try:
            seal = self.retained_completion.prepare(
                status.id,
                lifecycle_revision=status.lifecycle_revision,
                actor_type="controller",
                actor_identifier="ResearchWorkflowController",
                policy_version="research-controller-v1",
            )
        except CandidateBudgetOverrideRequired as exc:
            action = self.operator_actions.ensure_budget_action(status, exc.context)
            return self._directive(
                status,
                DISPOSITION_OPERATOR,
                action_kind=ACTION_BUDGET,
                action_id=action.action_id,
                diagnostics=[
                    "retained completion membership requires explicit candidate-budget authorization"
                ],
            )
        except CandidateBudgetHardRejected as exc:
            raise ControllerBlockedError(
                "retained completion membership violates a hard candidate-budget "
                f"limit: {bounded_text(exc)}"
            ) from exc
        except AssetPromotionError as exc:
            raise ControllerBlockedError(
                "retained completion membership could not be sealed: "
                f"{bounded_text(exc)}"
            ) from exc
        if (
            seal.run_id != status.id
            or seal.lifecycle_revision != status.lifecycle_revision
            or seal.status != "sealed"
            or not seal.members
        ):
            raise ControllerBlockedError(
                "retained completion membership returned contradictory authority"
            )
        return None

    def _terminalize_retained_only(self, status: RunStatus) -> RunStatus:
        if status.state == "retrieving":
            status = self._transition(
                status,
                "coverage_review",
                key=f"controller:retained-only-review:{status.id}",
                reason="retained-only review completed with insufficient authority",
            )
        if status.state != "coverage_review":
            raise ControllerBlockedError(
                f"cannot terminalize retained-only run from {status.state}"
            )
        try:
            self.run_service.partial(
                status.id,
                expected_revision=status.lifecycle_revision,
                idempotency_key=f"controller:retained-only-partial:{status.id}",
                actor_type="controller",
                actor_identifier="ResearchWorkflowController",
                triggering_event="run.retained_only_partial",
                reason="retained-only policy forbids provider acquisition",
                outcome="partial",
            )
        except StaleRunRevisionError:
            latest = self.run_service.status(run_id=status.id)
            if latest.state != "partial":
                raise
            return latest
        return self.run_service.status(run_id=status.id)

    def _close_retained_only_escape(self, status: RunStatus) -> RunStatus:
        if status.state == "indexing":
            try:
                self.run_service.partial(
                    status.id,
                    expected_revision=status.lifecycle_revision,
                    idempotency_key=(
                        f"controller:retained-only-indexing-escape:{status.id}"
                    ),
                    actor_type="controller",
                    actor_identifier="ResearchWorkflowController",
                    triggering_event="run.retained_only_partial",
                    reason=(
                        "retained-only policy encountered provider-derived "
                        "indexing state"
                    ),
                    outcome="partial",
                )
            except StaleRunRevisionError:
                pass
            return self.run_service.status(run_id=status.id)
        if status.state == "extracting":
            status = self._transition(
                status,
                "coverage_review",
                key=f"controller:retained-only-extraction-escape:{status.id}",
                reason="retained-only policy forbids provider scrape continuation",
            )
        if status.state == "acquiring":
            try:
                self.run_service.partial(
                    status.id,
                    expected_revision=status.lifecycle_revision,
                    idempotency_key=(
                        f"controller:retained-only-acquisition-escape:{status.id}"
                    ),
                    actor_type="controller",
                    actor_identifier="ResearchWorkflowController",
                    triggering_event="run.retained_only_partial",
                    reason="retained-only policy forbids provider acquisition",
                    outcome="partial",
                )
            except StaleRunRevisionError:
                pass
            return self.run_service.status(run_id=status.id)
        if status.state == "coverage_review":
            return self._terminalize_retained_only(status)
        return status

    def _resume_existing_orchestrator(
        self,
        status: RunStatus,
        bundle: PlanningBundle,
        *,
        delivery_mode: str = DELIVERY_SELF_SYNTHESIZED,
        stop_after_indexing: bool = False,
    ) -> OrchestratorResult:
        effective_caps = bundle.budget.get("effective_caps") or {}
        cycles = int(effective_caps.get("max_adaptive_cycles") or 0)
        if cycles < 1:
            raise ControllerBlockedError(
                "authoritative budget has no adaptive-cycle allowance"
            )
        external_id = status.external_id
        if external_id is None:
            raise ControllerBlockedError("run is missing public external identity")
        orchestrator = self.orchestrator_factory(
            OrchestratorConfig(
                execution_mode=status.execution_mode,
                budget_policy_version=str(bundle.budget["policy_version"]),
                max_adaptive_cycles=cycles,
            )
        )
        return orchestrator.run_from_external_id(
            external_id,
            spec=serialize_model(bundle.spec),
            search_plan=bundle.plan,
            max_adaptive_cycles=cycles,
            context={
                "spec_id": str(bundle.spec_row_id),
                "spec_revision": bundle.spec_revision,
                "search_plan_id": str(bundle.plan_row_id),
                "search_plan_revision": bundle.plan_revision,
                "authoritative_budget": bundle.budget,
                "delivery_mode": validate_delivery_mode(delivery_mode),
                **({"_stop_after_state": "indexing"} if stop_after_indexing else {}),
            },
        )

    def _response_from_orchestrator(
        self,
        external_id: str,
        result: OrchestratorResult,
    ) -> WorkflowDirective | ResearchResult:
        status = self.run_service.status(external_id=external_id)
        if status.state in TERMINAL_STATES:
            return self.result(external_id)

        action = getattr(result, "operator_action", None)
        if str(result.outcome) == "operator_action_required":
            if not isinstance(action, Mapping):
                raise ControllerBlockedError(
                    "orchestrator returned an untyped operator action"
                )
            kind = str(action.get("kind") or "")
            if kind == "candidate_budget_override_required":
                try:
                    context = CandidateBudgetAdmissionContext(
                        run_id=UUID(str(action["run_id"])),
                        lifecycle_revision=int(action["lifecycle_revision"]),
                        check_id=UUID(str(action["check_id"])),
                        scope=dict(action.get("scope") or {}),
                        scope_fingerprint=str(action["scope_fingerprint"]),
                        violated_limits=tuple(
                            sorted(
                                str(item)
                                for item in action.get("violated_limits") or ()
                            )
                        ),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ControllerBlockedError(
                        "orchestrator candidate-budget action is malformed"
                    ) from exc
                durable = self.operator_actions.ensure_budget_action(status, context)
            elif kind == "temporal_coverage_gap":
                durable = self.operator_actions.ensure_scope_action(status, action)
            else:
                raise ControllerBlockedError(
                    f"unsupported orchestrator operator action: {kind!r}"
                )
            return self._directive(
                status,
                DISPOSITION_OPERATOR,
                action_kind=durable.kind,
                action_id=durable.action_id,
                diagnostics=["a genuine human authorization boundary was reached"],
            )
        if result.error:
            return self._directive(
                status,
                DISPOSITION_FAILED,
                action_kind="inspect_failure",
                diagnostics=[result.error],
            )
        return self._directive(
            status,
            DISPOSITION_CONTINUE,
            action_kind="continue",
            diagnostics=[
                "controller stopped at a bounded resumable checkpoint"
                if str(result.outcome) in {"checkpoint", "resumable"}
                else "persisted run remains nonterminal"
            ],
        )

    @staticmethod
    def _tighten_guard_to_budget(
        guard: ProgressGuard,
        bundle: PlanningBundle,
    ) -> None:
        effective_caps = bundle.budget.get("effective_caps") or {}
        wall_clock = float(effective_caps.get("max_wall_clock_seconds") or 0)
        guard.tighten_deadline(wall_clock)

    def _transition(
        self,
        status: RunStatus,
        next_state: str,
        *,
        key: str,
        reason: str,
    ) -> RunStatus:
        if next_state == status.state:
            return status
        try:
            self.run_service.transition(
                status.id,
                next_state,
                expected_revision=status.lifecycle_revision,
                idempotency_key=key,
                actor_type="controller",
                actor_identifier="ResearchWorkflowController",
                triggering_event=f"run.{next_state}",
                reason=reason,
            )
        except StaleRunRevisionError:
            latest = self.run_service.status(run_id=status.id)
            if latest.state != next_state:
                raise
            return latest
        return self.run_service.status(run_id=status.id)

    def _fail_closed(self, status: RunStatus, reason: str) -> RunStatus:
        if status.state in TERMINAL_STATES:
            return status
        if "failed" not in PERMITTED_TRANSITIONS.get(status.state, ()):
            return status
        try:
            self.run_service.fail(
                status.id,
                expected_revision=status.lifecycle_revision,
                idempotency_key=(
                    f"controller:bound-failed:{status.id}:{status.lifecycle_revision}"
                ),
                actor_type="controller",
                actor_identifier="ResearchWorkflowController",
                triggering_event="run.controller_bound_failed",
                reason=reason,
                outcome="failed",
                error=reason,
            )
        except (RunStateError, StaleRunRevisionError):
            return self.run_service.status(run_id=status.id)
        return self.run_service.status(run_id=status.id)

    def _record_policy(self, status: RunStatus, policy: ControllerPolicy) -> None:
        with self.run_service.uow_factory() as uow:
            uow.runs.append_event(
                status.id,
                _CONTROLLER_POLICY_EVENT,
                "controller",
                f"controller:policy:{status.id}",
                actor_identifier="ResearchWorkflowController",
                payload={
                    "schema_version": CONTROLLER_POLICY_SCHEMA_VERSION,
                    "retained_only": policy.retained_only,
                    "curated": policy.curated,
                    "evaluated_at": policy.evaluated_at.isoformat(),
                    "delivery_mode": policy.delivery_mode,
                },
            )
            uow.commit()

    def _load_policy(self, status: RunStatus) -> ControllerPolicy:
        event = self._single_event(status.id, _CONTROLLER_POLICY_EVENT)
        if event is None:
            raise ControllerBlockedError(
                "run has no canonical controller policy; use the low-level surface "
                "or start it through fresearch"
            )
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping):
            raise ControllerBlockedError("persisted controller policy is malformed")
        if payload.get("schema_version") != CONTROLLER_POLICY_SCHEMA_VERSION:
            raise ControllerBlockedError("persisted controller policy is malformed")
        raw_retained_only = payload.get("retained_only")
        if not isinstance(raw_retained_only, bool):
            raise ControllerBlockedError(
                "persisted controller retained-only policy is malformed"
            )
        raw_curated = payload.get("curated")
        if not isinstance(raw_curated, bool):
            raise ControllerBlockedError(
                "persisted controller curated policy is malformed"
            )
        raw_delivery_mode = payload.get(
            "delivery_mode",
            DELIVERY_SELF_SYNTHESIZED,
        )
        try:
            delivery_mode = validate_delivery_mode(str(raw_delivery_mode))
        except ValueError as exc:
            raise ControllerBlockedError(
                "persisted controller delivery mode is malformed"
            ) from exc
        raw_evaluated_at = payload.get("evaluated_at")
        if not isinstance(raw_evaluated_at, str):
            raise ControllerBlockedError("persisted controller clock is missing")
        try:
            evaluated_at = datetime.fromisoformat(raw_evaluated_at)
        except ValueError as exc:
            raise ControllerBlockedError(
                "persisted controller clock is malformed"
            ) from exc
        if evaluated_at.tzinfo is None:
            raise ControllerBlockedError("persisted controller clock is timezone-naive")
        return ControllerPolicy(
            retained_only=raw_retained_only,
            curated=raw_curated,
            evaluated_at=evaluated_at.astimezone(timezone.utc),
            delivery_mode=delivery_mode,
        )

    def _build_public_handoff(
        self,
        status: RunStatus,
        delivery_mode: str,
    ) -> dict[str, Any]:
        external_id = status.external_id
        if external_id is None:
            raise ControllerBlockedError("run is missing public external identity")
        external_id = validate_public_run_id(external_id)
        delivery_mode = validate_delivery_mode(delivery_mode)

        payload = serialize_model(
            HandoffBuilder(self.run_service.uow_factory).build(status.id)
        )
        packet = payload.get("evidence_packet")
        spec = payload.get("research_spec")
        citation_ready = payload.get("citation_ready")
        packet_revision = int(payload.get("evidence_packet_revision") or 0)
        if (
            not isinstance(packet, dict)
            or packet.get("degraded")
            or packet_revision < 1
        ):
            raise ControllerBlockedError(
                "terminal handoff requires a persisted EvidencePacket"
            )
        if not isinstance(spec, dict) or not spec:
            raise ControllerBlockedError(
                "terminal handoff requires a persisted ResearchSpec"
            )
        if not isinstance(citation_ready, dict):
            raise ControllerBlockedError(
                "terminal handoff requires bounded citation-ready evidence"
            )

        packet_sha256 = hashlib.sha256(
            json.dumps(
                packet,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        packet_coverage_revision = int(packet.get("coverage_revision") or 0)
        if packet_coverage_revision < 1:
            raise ControllerBlockedError(
                "terminal handoff EvidencePacket has no authoritative coverage revision"
            )
        with self.run_service.uow_factory() as uow:
            coverage_snapshot = uow.coverage.get_snapshot(
                status.id, packet_coverage_revision
            )
        if coverage_snapshot is None:
            raise ControllerBlockedError(
                "terminal handoff requires the EvidencePacket-bound coverage snapshot"
            )
        coverage_ledger = coverage_snapshot.get("ledger")
        if not isinstance(coverage_ledger, dict):
            raise ControllerBlockedError(
                "terminal handoff coverage snapshot is malformed"
            )
        if (
            int(coverage_snapshot.get("coverage_revision") or 0)
            != packet_coverage_revision
        ):
            raise ControllerBlockedError(
                "terminal handoff coverage authority contradicts the EvidencePacket"
            )

        coverage_items = list(coverage_ledger.get("items") or ())
        coverage_by_id = {
            str(item.get("coverage_item_id")): item
            for item in coverage_items
            if isinstance(item, dict) and item.get("coverage_item_id")
        }
        unresolved_items: list[dict[str, Any]] = []
        for index, unresolved_id in enumerate(
            packet.get("unresolved_items") or (), start=1
        ):
            item = coverage_by_id.get(str(unresolved_id))
            if item is None:
                raise ControllerBlockedError(
                    "EvidencePacket unresolved item is absent from its coverage snapshot"
                )
            unresolved_items.append(
                {
                    "unresolved_ref": f"unresolved_{index}",
                    "item_type": item.get("item_type"),
                    "status": item.get("status"),
                    "freshness_status": item.get("freshness_status"),
                    "remaining_gap": item.get("remaining_gap"),
                }
            )

        completion_audit: dict[str, Any] | None = None
        if status.state == "completed":
            with self.run_service.uow_factory() as uow:
                completion = load_authoritative_completion_provenance(uow, status.id)
            if completion.run_id != status.id:
                raise ControllerBlockedError(
                    "completion provenance belongs to another research run"
                )
            if completion.evidence_packet_revision != packet_revision:
                raise ControllerBlockedError(
                    "terminal handoff EvidencePacket revision is not the completed revision"
                )
            if completion.evidence_packet_sha256 != packet_sha256:
                raise ControllerBlockedError(
                    "terminal handoff EvidencePacket hash is not the completed packet"
                )
            completion_audit = completion.audit_metadata()
            if (
                delivery_mode == DELIVERY_HOST_HANDOFF
                and completion_audit.get("delivery_mode") != DELIVERY_HOST_HANDOFF
            ):
                raise ControllerBlockedError(
                    "host handoff completion provenance uses the wrong delivery mode"
                )

        raw_claims = list(citation_ready.get("claims") or ())
        raw_passages = list(citation_ready.get("passages") or ())
        claim_refs = {
            str(item.get("claim_id")): f"claim_{index}"
            for index, item in enumerate(raw_claims, start=1)
            if isinstance(item, dict) and item.get("claim_id")
        }
        passage_refs = {
            str(item.get("passage_id")): f"passage_{index}"
            for index, item in enumerate(raw_passages, start=1)
            if isinstance(item, dict) and item.get("passage_id")
        }
        claims = [
            {
                "claim_ref": claim_refs[str(item["claim_id"])],
                "statement": item.get("statement"),
                "semantic_status": item.get("semantic_status"),
                "uncertainty": item.get("uncertainty"),
            }
            for item in raw_claims
            if isinstance(item, dict) and str(item.get("claim_id")) in claim_refs
        ]
        passages = [
            {
                "passage_ref": passage_refs[str(item["passage_id"])],
                "text": item.get("text"),
                "source_url": item.get("source_url"),
            }
            for item in raw_passages
            if isinstance(item, dict) and str(item.get("passage_id")) in passage_refs
        ]
        bindings: list[dict[str, Any]] = []
        for claim_id, values in dict(citation_ready.get("bindings") or {}).items():
            claim_ref = claim_refs.get(str(claim_id))
            if claim_ref is None:
                continue
            for value in values or ():
                if not isinstance(value, dict):
                    continue
                refs = [
                    passage_refs[str(item)]
                    for item in value.get("passage_ids") or ()
                    if str(item) in passage_refs
                ]
                if refs:
                    bindings.append(
                        {
                            "claim_ref": claim_ref,
                            "passage_refs": refs,
                            "relationship": value.get("relationship"),
                            "confidence": value.get("confidence"),
                        }
                    )

        safe_spec: dict[str, Any] = {
            key: spec.get(key)
            for key in (
                "schema_version",
                "objective",
                "research_archetype",
                "risk_level",
                "execution_mode",
                "entities",
                "jurisdictions",
                "time_window",
                "excluded_interpretations",
                "user_constraints",
                "ambiguities",
                "assumptions",
            )
            if key in spec
        }
        safe_spec["questions"] = [
            item.get("text")
            for item in spec.get("questions") or ()
            if isinstance(item, dict) and item.get("text")
        ]
        safe_spec["claims_to_validate"] = [
            item.get("statement")
            for item in spec.get("claims_to_validate") or ()
            if isinstance(item, dict) and item.get("statement")
        ]
        safe_spec["freshness_requirements"] = [
            {
                "description": item.get("description"),
                "max_age_days": item.get("max_age_days"),
            }
            for item in spec.get("freshness_requirements") or ()
            if isinstance(item, dict)
        ]
        safe_spec["required_source_classes"] = [
            {
                "source_class": item.get("source_class"),
                "minimum_count": item.get("minimum_count"),
            }
            for item in spec.get("required_source_classes") or ()
            if isinstance(item, dict)
        ]
        for field in ("corroboration_requirements", "contradiction_requirements"):
            safe_spec[field] = [
                {
                    "description": item.get("description"),
                    "required_independent_source_count": item.get(
                        "required_independent_source_count"
                    ),
                }
                for item in spec.get(field) or ()
                if isinstance(item, dict)
            ]
        safe_spec["structured_data_requirements"] = [
            {
                "description": item.get("description"),
                "required_fields": list(item.get("required_fields") or ()),
            }
            for item in spec.get("structured_data_requirements") or ()
            if isinstance(item, dict)
        ]
        safe_spec["completion_criteria"] = [
            {
                "description": item.get("description"),
                "mandatory": item.get("mandatory"),
            }
            for item in spec.get("completion_criteria") or ()
            if isinstance(item, dict)
        ]

        status_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        for item in coverage_items:
            if not isinstance(item, dict):
                continue
            status_name = str(item.get("status") or "unknown")
            item_type = str(item.get("item_type") or "unknown")
            status_counts[status_name] = status_counts.get(status_name, 0) + 1
            type_counts[item_type] = type_counts.get(item_type, 0) + 1
        safe_coverage = {
            "schema_version": str(
                coverage_ledger.get("schema_version") or "coverage-ledger-v1"
            ),
            "coverage_revision": packet_coverage_revision,
            "total_items": len(coverage_items),
            "status_counts": dict(sorted(status_counts.items())),
            "type_counts": dict(sorted(type_counts.items())),
            "overall_status": coverage_ledger.get("overall_status"),
        }

        authority: dict[str, Any] = {
            "evidence_packet_revision": packet_revision,
            "evidence_packet_sha256": packet_sha256,
        }
        if completion_audit is not None:
            authority["completion_schema_version"] = completion_audit.get(
                "schema_version"
            )
            if delivery_mode == DELIVERY_HOST_HANDOFF:
                authority["handoff_authority_sha256"] = completion_audit.get(
                    "handoff_authority_sha256"
                )
            else:
                authority["synthesis_artifact_sha256"] = completion_audit.get(
                    "synthesis_artifact_sha256"
                )

        packet_limitations = [str(item) for item in packet.get("limitations") or ()]
        return {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "run_id": external_id,
            "delivery_mode": delivery_mode,
            "objective_authority": safe_spec,
            "coverage": {
                **safe_coverage,
                "lifecycle_state": status.state,
                "declared_outcome": status.declared_outcome,
                "objective_satisfied": status.state == "completed",
            },
            "citation_ready": {
                "claims": claims,
                "passages": passages,
                "bindings": bindings,
                "metadata": {
                    "coverage_revision": packet.get("coverage_revision"),
                    "claim_count": len(packet.get("claims") or ()),
                    "passage_count": len(packet.get("passages") or ()),
                    "omitted_passage_count": len(packet.get("omitted_passages") or ()),
                    "binding_count": len(packet.get("claim_evidence_bindings") or ()),
                    "source_diversity": packet.get("source_diversity_summary"),
                },
            },
            "temporal_qualification": {
                "research_spec_time_window": safe_spec.get("time_window"),
                "freshness_requirements": safe_spec.get("freshness_requirements") or [],
                "evidence_freshness": packet.get("freshness_summary") or {},
            },
            "limitations": list(
                dict.fromkeys(
                    [
                        *packet_limitations,
                        *[str(item) for item in payload.get("limitations") or ()],
                    ]
                )
            ),
            "unresolved_item_count": len(unresolved_items),
            "unresolved_items": unresolved_items,
            "authority": authority,
        }

    def _single_event(self, run_id: UUID, event_type: str) -> dict[str, Any] | None:
        with self.run_service.uow_factory() as uow:
            events = uow.runs.list_events(
                run_id,
                event_type=event_type,
                limit=_MAX_EVENT_READ,
                offset=0,
            )
        if not events:
            return None
        if len(events) > 1:
            raise ControllerBlockedError(
                f"multiple authoritative {event_type} events exist for one run"
            )
        return events[0]

    def _acquisition_wave_count(self, run_id: UUID) -> int:
        with self.run_service.uow_factory() as uow:
            return int(uow.runs.count_acquisition_waves(run_id))

    def _handoff_ready(self, status: RunStatus) -> bool:
        if status.state not in {"completed", "partial"}:
            return False
        try:
            policy = self._load_policy(status)
            self._build_public_handoff(status, policy.delivery_mode)
        except (
            CompletionProvenanceError,
            ControllerBlockedError,
            KeyError,
            TypeError,
            ValueError,
        ):
            return False
        return True

    def _directive(
        self,
        status: RunStatus,
        disposition: str,
        *,
        action_kind: str | None = None,
        action_id: str | None = None,
        diagnostics: list[Any] | None = None,
        limitations: list[Any] | None = None,
        handoff_ready: bool = False,
    ) -> WorkflowDirective:
        external_id = status.external_id
        if external_id is None:
            raise ControllerBlockedError("run is missing public external identity")
        terminal = status.state in TERMINAL_STATES
        return WorkflowDirective(
            schema_version=DIRECTIVE_SCHEMA_VERSION,
            run_id=validate_public_run_id(external_id),
            lifecycle_state=status.state,
            lifecycle_revision=status.lifecycle_revision,
            disposition=disposition,
            action_kind=action_kind,
            action_id=action_id,
            diagnostics=bounded_messages(diagnostics or []),
            limitations=bounded_messages(limitations or []),
            result_ready=(terminal and (status.state != "completed" or handoff_ready)),
            handoff_ready=handoff_ready,
            objective_satisfied=status.state == "completed",
        )


def build_research_controller(
    config: Any | None = None,
    *,
    query_planner: QueryPlanner | None = None,
    controller_config: ControllerConfig | None = None,
) -> ResearchWorkflowController:
    """Compose the controller only from the repository's canonical builders."""
    from .composition import (
        build_evidence_service,
        build_invocation_service,
        build_production_resumable_orchestrator,
        build_run_service,
        build_semantic_service,
        build_service,
    )
    from .config import StoreConfig
    from .coverage_seed_service import CompleteCoverageService

    resolved = config or StoreConfig.from_env()
    resolved.require_database()
    run_service = build_run_service(resolved)
    coverage_service = CompleteCoverageService(run_service.uow_factory)
    return ResearchWorkflowController(
        config=resolved,
        run_service=run_service,
        invocation_service=build_invocation_service(resolved),
        corpus_service=build_service(resolved),
        coverage_service=coverage_service,
        evidence_service=build_evidence_service(resolved),
        semantic_service=build_semantic_service(resolved),
        orchestrator_factory=lambda orchestrator_config: (
            build_production_resumable_orchestrator(
                resolved,
                orchestrator_config=orchestrator_config,
            )
        ),
        query_planner=query_planner,
        controller_config=controller_config,
    )


__all__ = [
    "ControllerPolicy",
    "ResearchWorkflowController",
    "build_research_controller",
    "default_query_planner",
]
