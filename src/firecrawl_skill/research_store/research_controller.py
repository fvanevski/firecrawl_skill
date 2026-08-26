"""Canonical deterministic application controller for research workflow issue #310."""

from __future__ import annotations

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
from .invocation_service import InvocationError, InvocationRecord, InvocationService
from .operator_action_service import (
    ACTION_BUDGET,
    ACTION_CURATION,
    ACTION_SCOPE,
    OperatorActionError,
    OperatorActionService,
)
from .orchestrator import OrchestratorConfig, OrchestratorResult
from .research_controller_contract import (
    DIRECTIVE_SCHEMA_VERSION,
    DISPOSITION_BLOCKED,
    DISPOSITION_CONTINUE,
    DISPOSITION_FAILED,
    DISPOSITION_OPERATOR,
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
_PLANNING_INPUT_SCHEMA = "fresearch-planning-input-v1"
_PLANNING_OUTPUT_SCHEMA = "fresearch-planning-result-v1"
_MAX_EVENT_READ = 2


@dataclass(frozen=True)
class ControllerPolicy:
    retained_only: bool
    curated: bool
    evaluated_at: datetime


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
                        selection = self.retained_review.ensure_selection(status, bundle)
                        if selection:
                            action = self.operator_actions.ensure_curation_action(status)
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
        ready = self._handoff_ready(status.id, status.state)
        if status.state in TERMINAL_STATES:
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
        except ControllerBlockedError as exc:
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
        handoff_ready = self._handoff_ready(status.id, status.state)
        return ResearchResult(
            schema_version=RESULT_SCHEMA_VERSION,
            run_id=external_id,
            objective=status.objective,
            lifecycle_state=status.state,
            lifecycle_revision=status.lifecycle_revision,
            disposition=directive.disposition,
            terminal=terminal,
            outcome=status.declared_outcome,
            result_ready=terminal,
            handoff_ready=handoff_ready,
            objective_satisfied=status.state == "completed",
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
                            sorted(str(item) for item in action.get("violated_limits") or ())
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
                    "schema_version": "research-controller-policy-v1",
                    "retained_only": policy.retained_only,
                    "evaluated_at": policy.evaluated_at.isoformat(),
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
        if payload.get("schema_version") != "research-controller-policy-v1":
            raise ControllerBlockedError("persisted controller policy is malformed")
        raw_retained_only = payload.get("retained_only")
        if not isinstance(raw_retained_only, bool):
            raise ControllerBlockedError(
                "persisted controller retained-only policy is malformed"
            )
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
            evaluated_at=evaluated_at.astimezone(timezone.utc),
        )

    def _record_operator_action(self, status: RunStatus, action_kind: str) -> None:
        with self.run_service.uow_factory() as uow:
            uow.runs.append_event(
                status.id,
                _OPERATOR_ACTION_EVENT,
                "controller",
                (
                    f"controller:operator-action:{status.id}:"
                    f"r{status.lifecycle_revision}:{action_kind}"
                ),
                actor_identifier="ResearchWorkflowController",
                payload={
                    "schema_version": "controller-operator-action-v1",
                    "action_kind": action_kind,
                    "lifecycle_revision": status.lifecycle_revision,
                    "internal_parameters_exposed": False,
                },
            )
            uow.commit()

    def _active_operator_action(self, status: RunStatus) -> str | None:
        with self.run_service.uow_factory() as uow:
            events = uow.runs.list_events(
                status.id,
                event_type=_OPERATOR_ACTION_EVENT,
                limit=_MAX_OPERATOR_ACTION_EVENTS + 1,
                offset=0,
            )
        if len(events) > _MAX_OPERATOR_ACTION_EVENTS:
            raise ControllerBlockedError(
                "controller operator-action history exceeds its bounded read"
            )
        if not events:
            return None
        payload = events[-1].get("payload") or {}
        if not isinstance(payload, Mapping):
            raise ControllerBlockedError(
                "persisted controller operator action is malformed"
            )
        raw_revision = payload.get("lifecycle_revision")
        if raw_revision is None:
            raise ControllerBlockedError(
                "persisted controller operator action revision is malformed"
            )
        try:
            revision = int(raw_revision)
        except (TypeError, ValueError) as exc:
            raise ControllerBlockedError(
                "persisted controller operator action revision is malformed"
            ) from exc
        if revision != status.lifecycle_revision:
            return None
        action_kind = str(payload.get("action_kind") or "")
        return action_kind or None

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

    def _handoff_ready(self, run_id: UUID, state: str) -> bool:
        if state not in {"completed", "partial"}:
            return False
        with self.run_service.uow_factory() as uow:
            packet = uow.evidence_packets.get_evidence_packet(run_id)
        return packet is not None

    def _directive(
        self,
        status: RunStatus,
        disposition: str,
        *,
        action_kind: str | None = None,
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
            diagnostics=bounded_messages(diagnostics or []),
            limitations=bounded_messages(limitations or []),
            result_ready=terminal,
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
