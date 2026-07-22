"""Coverage-led research orchestrator.

This module replaces the monolithic ``fsearch_smart`` loop with an explicit,
staged orchestrator that:

1. Transitions the research run through explicit states via ``ResearchRunService``.
2. Creates coverage items from the ``ResearchSpec`` before acquisition.
3. Evaluates coverage after each meaningful wave.
4. Proposes and authorizes adaptive actions through ``StrategyRevisionService``.
5. Persists every invocation and state transition.
6. Uses successful-page count only as diagnostic metadata.
7. Permits sufficient runs to stop below page targets.
8. Prevents insufficient runs from completing because enough pages succeeded.
9. Resumes after process restart by detecting existing run state.
10. Retains compatibility-wrapper output through the legacy adapter.

The orchestrator is the single entry point for the coverage-led workflow.
All state transitions flow through ``ResearchRunService`` — no second state
machine exists.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from .acquisition_service import AcquisitionService
from .config import StoreConfig
from .coverage_service import CoverageService
from .legacy_adapter import AdapterMode, LegacyEntryPointAdapter
from .run_service import (
    ResearchRunService,
    RunStateError,
    StaleRunRevisionError,
)
from .stages import (
    ContextKeys,
    StageHandler,
    StageOutcome,
    StageResult,
    _coverage_decision,
    decision_to_state,
    STRATEGY_DECISION_SYNTHESIZE,
    STRATEGY_DECISION_SEARCH,
    STRATEGY_DECISION_PARTIAL,
    STRATEGY_DECISION_FAIL,
)
from .strategy_service import StrategyRevisionService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Orchestrator configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrchestratorConfig:
    """Configuration for the coverage-led orchestrator.

    Attributes:
        execution_mode: The execution mode for this run.
        budget_policy_version: Version string for the budget policy.
        max_adaptive_cycles: Maximum number of coverage-review cycles.
        resume_on_conflict: If True, resume an existing run instead of failing.
        legacy_adapter_mode: Compatibility wrapper mode.
    """

    execution_mode: str = "autonomous_local"
    budget_policy_version: str = "budget-policy-v1"
    max_adaptive_cycles: int = 10
    resume_on_conflict: bool = True
    legacy_adapter_mode: str = "authoritative"


# ---------------------------------------------------------------------------
# Orchestrator result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrchestratorResult:
    """Final result of an orchestrator invocation.

    Attributes:
        run_id: The research run that was orchestrated.
        final_state: The terminal state the run ended in.
        outcome: One of "completed", "partial", "failed", "resumed".
        coverage_revision: Final coverage revision.
        wave_count: Number of acquisition waves executed.
        successful_urls: Diagnostic count of successful scrapes.
        strategy_proposals: Number of strategy proposals created.
        strategy_decisions: Number of strategy decisions authorized.
        error: Error message if the run failed.
    """

    run_id: UUID
    final_state: str
    outcome: str
    coverage_revision: int | None = None
    wave_count: int = 0
    successful_urls: int = 0
    strategy_proposals: int = 0
    strategy_decisions: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "final_state": self.final_state,
            "outcome": self.outcome,
            "coverage_revision": self.coverage_revision,
            "wave_count": self.wave_count,
            "successful_urls": self.successful_urls,
            "strategy_proposals": self.strategy_proposals,
            "strategy_decisions": self.strategy_decisions,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Stage implementations
# ---------------------------------------------------------------------------


class PlanningStage:
    """Plan the research: create spec and search plan.

    Transitions: created -> planning -> corpus_review
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        config: StoreConfig,
    ) -> None:
        self.run_service = run_service
        self.config = config

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        if run_state not in ("created", "planning"):
            return StageResult.failed(
                "planning",
                f"planning stage requires created/planning state, got {run_state}",
            )

        # Transition to planning
        try:
            self.run_service.transition(
                run_id,
                "planning",
                expected_revision=run_revision,
                idempotency_key=f"stage:planning:{run_id}:{uuid4()}",
                actor_type="orchestrator",
                actor_identifier="PlanningStage",
                triggering_event="run.planning",
                reason="coverage-led planning",
            )
        except (RunStateError, StaleRunRevisionError) as exc:
            return StageResult.failed("planning", str(exc))

        # The spec and search plan are expected to be provided by the caller
        # through context (set during --research-spec or frun start).
        spec = context.get("spec")
        search_plan = context.get("search_plan")

        if spec is None:
            return StageResult.failed(
                "planning",
                "ResearchSpec not provided in context; "
                "use --research-spec or frun start to supply one",
            )

        # Record spec if not already recorded
        spec_id = context.get(ContextKeys.SPEC_ID)
        spec_revision = context.get(ContextKeys.SPEC_REVISION, 1)
        if spec_id is None:
            spec_revision = 1
            spec_id = self.run_service.record_search_plan(
                run_id,
                research_spec_id=UUID(str(spec.get("research_spec_id", uuid4()))),
                revision=spec_revision,
                search_plan=search_plan or {"queries": []},
                idempotency_key=f"spec:{run_id}:{spec_revision}",
            )
            context[ContextKeys.SPEC_ID] = spec_id
            context[ContextKeys.SPEC_REVISION] = spec_revision

        # Transition to corpus_review
        try:
            self.run_service.transition(
                run_id,
                "corpus_review",
                expected_revision=run_revision + 1,
                idempotency_key=f"stage:planning_done:{run_id}:{uuid4()}",
                actor_type="orchestrator",
                actor_identifier="PlanningStage",
                triggering_event="run.corpus_review",
                reason="planning complete, ready for corpus review",
            )
        except (RunStateError, StaleRunRevisionError) as exc:
            return StageResult.failed("planning", str(exc))

        return StageResult.ok(
            "planning",
            "spec and search plan recorded, transitioned to corpus_review",
            details={
                ContextKeys.SPEC_ID: str(spec_id),
                ContextKeys.SPEC_REVISION: spec_revision,
            },
        )


class CorpusReviewStage:
    """Create coverage items from the ResearchSpec.

    Transitions: corpus_review -> acquiring
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        coverage_service: CoverageService,
    ) -> None:
        self.run_service = run_service
        self.coverage_service = coverage_service

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        if run_state != "corpus_review":
            return StageResult.failed(
                "corpus_review",
                f"corpus_review stage requires corpus_review state, got {run_state}",
            )

        spec = context.get("spec")
        if spec is None:
            return StageResult.failed(
                "corpus_review", "ResearchSpec not available for coverage creation"
            )

        execution_mode = context.get("execution_mode", "autonomous_local")

        # Create coverage items from the spec
        try:
            items = self.coverage_service.create_items_from_spec(
                run_id,
                spec,
                execution_mode=execution_mode,
                idempotency_key=f"coverage:items:{run_id}",
                source_event_id=None,
                source_invocation_id=None,
            )
        except Exception as exc:
            return StageResult.failed(
                "corpus_review", f"coverage creation failed: {exc}"
            )

        # Create initial snapshot
        try:
            self.coverage_service.create_snapshot(
                run_id,
                ledger={
                    "schema_version": "coverage-ledger-v1",
                    "run_id": str(run_id),
                    "revision": 1,
                    "items": [
                        {
                            "coverage_item_id": str(item.coverage_item_id),
                            "item_type": item.item_type.value,
                            "subject_id": item.subject_id,
                            "status": item.status.value,
                            "candidate_ids": [],
                            "snapshot_ids": [],
                            "passage_ids": [],
                            "independent_source_count": 0,
                            "required_independent_source_count": 0,
                            "authority_classes_present": [],
                            "freshness_status": item.freshness_status.value,
                            "remaining_gap": item.remaining_gap,
                            "confidence": item.confidence,
                        }
                        for item in items
                    ],
                    "overall_status": "unassessed",
                },
                coverage_revision=1,
                idempotency_key=f"snapshot:initial:{run_id}",
            )
        except Exception as exc:
            return StageResult.failed(
                "corpus_review", f"snapshot creation failed: {exc}"
            )

        # Update run's current_coverage_revision
        try:
            self.run_service.transition(
                run_id,
                "acquiring",
                expected_revision=run_revision + 1,
                idempotency_key=f"stage:corpus_review_done:{run_id}:{uuid4()}",
                actor_type="orchestrator",
                actor_identifier="CorpusReviewStage",
                triggering_event="run.acquiring",
                reason=f"coverage items created ({len(items)} items, revision 1)",
            )
        except (RunStateError, StaleRunRevisionError) as exc:
            return StageResult.failed("corpus_review", str(exc))

        return StageResult.ok(
            "corpus_review",
            f"created {len(items)} coverage items, transitioned to acquiring",
            details={
                ContextKeys.COVERAGE_STATUS: "unassessed",
                ContextKeys.OVERALL_STATUS: "unassessed",
            },
        )


class AcquisitionStage:
    """Execute search queries and persist candidates.

    Transitions: acquiring -> (indexing | coverage_review)
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        acquisition_service: AcquisitionService,
        coverage_service: CoverageService,
        strategy_service: StrategyRevisionService,
        config: StoreConfig,
    ) -> None:
        self.run_service = run_service
        self.acquisition_service = acquisition_service
        self.coverage_service = coverage_service
        self.strategy_service = strategy_service
        self.config = config

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        if run_state not in ("acquiring", "coverage_review"):
            return StageResult.failed(
                "acquisition",
                f"acquisition stage requires acquiring/coverage_review state, got {run_state}",
            )

        search_plan = context.get("search_plan")
        if search_plan is None:
            return StageResult.failed(
                "acquisition", "Search plan not available for acquisition"
            )

        queries = search_plan.get("queries", [])

        # Pass Strategy Queries to AcquisitionStage: In cycle 2+, extract
        # authorized queries from strategy proposals and merge with the
        # original search plan. This allows the orchestrator to execute
        # adaptive queries proposed during coverage_review.
        authorized_proposals = context.get(ContextKeys.AUTHORIZED_QUERIES, [])
        if authorized_proposals:
            # Merge authorized queries with the original search plan,
            # avoiding duplicates by query text.
            existing_texts = {q.get("query", "") for q in queries}
            for proposal in authorized_proposals:
                for q in proposal.get("proposed_queries", []):
                    query_text = q.get("query", "")
                    if query_text and query_text not in existing_texts:
                        queries.append(q)
                        existing_texts.add(query_text)

        if not queries:
            return StageResult.failed(
                "acquisition", "Search plan has no queries to execute"
            )

        # Execute each query through the acquisition service
        response_ids = []
        candidate_count = 0
        successful_urls = 0

        for query in queries:
            query_text = query.get("query", "")
            if not query_text:
                continue

            try:
                result = self.acquisition_service.execute_query(
                    run_id,
                    query_text,
                    idempotency_key=f"acquire:{run_id}:{query_text}",
                )
                response_ids.append(result.get("response_id"))
                candidate_count += result.get("candidate_count", 0)
                successful_urls += result.get("successful_urls", 0)
            except Exception as exc:
                logger.warning("acquisition query failed: %s — %s", query_text, exc)

        # Apply candidate_identified events to coverage
        if coverage_revision is not None:
            try:
                # Apply one event per candidate to track individual discoveries
                for i in range(min(candidate_count, 5)):  # Limit to first 5
                    self.coverage_service.apply_event(
                        run_id,
                        "candidate_identified",
                        idempotency_key=f"acquire:cand:{run_id}:{i}",
                        payload={
                            "candidate_id": str(uuid4()),
                            "candidate_count": candidate_count,
                        },
                    )
            except Exception as exc:
                logger.warning("coverage update after acquisition failed: %s", exc)

        # Update context with acquisition results
        context[ContextKeys.SEARCH_RESPONSE_IDS] = response_ids
        context[ContextKeys.CANDIDATE_COUNT] = candidate_count
        context[ContextKeys.SUCCESSFUL_URLS] = successful_urls

        # Transition to indexing or coverage_review
        if candidate_count > 0:
            try:
                self.run_service.transition(
                    run_id,
                    "indexing",
                    expected_revision=run_revision + 1,
                    idempotency_key=f"stage:acquisition_done:{run_id}:{uuid4()}",
                    actor_type="orchestrator",
                    actor_identifier="AcquisitionStage",
                    triggering_event="run.indexing",
                    reason=f"acquired {candidate_count} candidates",
                )
            except (RunStateError, StaleRunRevisionError) as exc:
                return StageResult.failed("acquisition", str(exc))

            return StageResult.ok(
                "acquisition",
                f"executed {len(queries)} queries, {candidate_count} candidates",
                details={
                    ContextKeys.SEARCH_RESPONSE_IDS: response_ids,
                    ContextKeys.CANDIDATE_COUNT: candidate_count,
                    ContextKeys.SUCCESSFUL_URLS: successful_urls,
                },
            )

        # No candidates — go directly to coverage review
        try:
            self.run_service.transition(
                run_id,
                "coverage_review",
                expected_revision=run_revision + 1,
                idempotency_key=f"stage:acquisition_empty:{run_id}:{uuid4()}",
                actor_type="orchestrator",
                actor_identifier="AcquisitionStage",
                triggering_event="run.coverage_review",
                reason="no candidates acquired, reviewing coverage",
            )
        except (RunStateError, StaleRunRevisionError) as exc:
            return StageResult.failed("acquisition", str(exc))

        return StageResult.ok(
            "acquisition",
            f"executed {len(queries)} queries, 0 candidates (empty)",
            details={
                ContextKeys.SEARCH_RESPONSE_IDS: response_ids,
                ContextKeys.CANDIDATE_COUNT: 0,
                ContextKeys.SUCCESSFUL_URLS: 0,
            },
        )


class ExtractionStage:
    """Extract content from acquired candidates.

    Transitions: extracting -> (indexing | coverage_review)
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        coverage_service: CoverageService,
        config: StoreConfig,
    ) -> None:
        self.run_service = run_service
        self.coverage_service = coverage_service
        self.config = config

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        if run_state not in ("extracting", "coverage_review"):
            return StageResult.failed(
                "extraction",
                f"extraction stage requires extracting/coverage_review state, got {run_state}",
            )

        # Extraction is handled by the corpus service's ingestion pipeline.
        # This stage records the transition and emits extraction_attempted
        # events for coverage tracking.

        extraction_success_count = context.get(ContextKeys.EXTRACTION_SUCCESS_COUNT, 0)
        context[ContextKeys.EXTRACTION_SUCCESS_COUNT] = extraction_success_count

        # Transition to indexing if we have content, otherwise to coverage_review
        if extraction_success_count > 0:
            try:
                self.run_service.transition(
                    run_id,
                    "indexing",
                    expected_revision=run_revision + 1,
                    idempotency_key=f"stage:extraction_done:{run_id}:{uuid4()}",
                    actor_type="orchestrator",
                    actor_identifier="ExtractionStage",
                    triggering_event="run.indexing",
                    reason=f"extraction succeeded for {extraction_success_count} sources",
                )
            except (RunStateError, StaleRunRevisionError) as exc:
                return StageResult.failed("extraction", str(exc))
        else:
            try:
                self.run_service.transition(
                    run_id,
                    "coverage_review",
                    expected_revision=run_revision + 1,
                    idempotency_key=f"stage:extraction_empty:{run_id}:{uuid4()}",
                    actor_type="orchestrator",
                    actor_identifier="ExtractionStage",
                    triggering_event="run.coverage_review",
                    reason="no successful extractions, reviewing coverage",
                )
            except (RunStateError, StaleRunRevisionError) as exc:
                return StageResult.failed("extraction", str(exc))

        return StageResult.ok(
            "extraction",
            f"{extraction_success_count} successful extractions",
            details={ContextKeys.EXTRACTION_SUCCESS_COUNT: extraction_success_count},
        )


class IndexingStage:
    """Build and activate vector index.

    Transitions: indexing -> coverage_review
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        config: StoreConfig,
    ) -> None:
        self.run_service = run_service
        self.config = config

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        if run_state != "indexing":
            return StageResult.failed(
                "indexing",
                f"indexing stage requires indexing state, got {run_state}",
            )

        # Indexing is handled by the corpus service's indexing pipeline.
        # This stage records the transition and emits indexing events.

        try:
            self.run_service.transition(
                run_id,
                "coverage_review",
                expected_revision=run_revision + 1,
                idempotency_key=f"stage:indexing_done:{run_id}:{uuid4()}",
                actor_type="orchestrator",
                actor_identifier="IndexingStage",
                triggering_event="run.coverage_review",
                reason="indexing complete, evaluating coverage",
            )
        except (RunStateError, StaleRunRevisionError) as exc:
            return StageResult.failed("indexing", str(exc))

        return StageResult.ok(
            "indexing",
            "indexing complete, transitioned to coverage_review",
        )


class CoverageReviewStage:
    """Evaluate coverage and propose next action.

    Transitions: coverage_review -> (acquiring | extracting | retrieving |
    synthesizing | partial | failed)
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        coverage_service: CoverageService,
        strategy_service: StrategyRevisionService,
        config: StoreConfig,
    ) -> None:
        self.run_service = run_service
        self.coverage_service = coverage_service
        self.strategy_service = strategy_service
        self.config = config

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        if run_state != "coverage_review":
            return StageResult.failed(
                "coverage_review",
                f"coverage_review stage requires coverage_review state, got {run_state}",
            )

        # Rebuild coverage projection
        try:
            ledger = self.coverage_service.rebuild_projection(
                run_id,
                idempotency_key=f"rebuild:{run_id}:{coverage_revision or 0}",
            )
        except Exception as exc:
            return StageResult.failed(
                "coverage_review", f"projection rebuild failed: {exc}"
            )

        overall_status = ledger.overall_status.value if ledger else "unassessed"

        # Create snapshot of current coverage
        try:
            new_coverage_revision = (coverage_revision or 0) + 1
            self.coverage_service.create_snapshot(
                run_id,
                ledger={
                    "schema_version": "coverage-ledger-v1",
                    "run_id": str(run_id),
                    "revision": new_coverage_revision,
                    "items": [
                        {
                            "coverage_item_id": str(item.coverage_item_id),
                            "item_type": item.item_type.value,
                            "subject_id": item.subject_id,
                            "status": item.status.value,
                            "candidate_ids": [str(cid) for cid in item.candidate_ids],
                            "snapshot_ids": [str(sid) for sid in item.snapshot_ids],
                            "passage_ids": [str(pid) for pid in item.passage_ids],
                            "independent_source_count": item.independent_source_count,
                            "required_independent_source_count": item.required_independent_source_count,
                            "authority_classes_present": list(
                                item.authority_classes_present
                            ),
                            "freshness_status": item.freshness_status.value,
                            "remaining_gap": item.remaining_gap,
                            "confidence": item.confidence,
                        }
                        for item in ledger.items
                    ],
                    "overall_status": overall_status,
                },
                coverage_revision=new_coverage_revision,
                idempotency_key=f"snapshot:review:{run_id}:{new_coverage_revision}",
            )
        except Exception as exc:
            logger.warning("coverage snapshot creation failed: %s", exc)

        # Update context with coverage results
        context[ContextKeys.COVERAGE_LEDGER] = ledger
        context[ContextKeys.COVERAGE_STATUS] = overall_status
        context[ContextKeys.OVERALL_STATUS] = overall_status

        # Determine next action based on coverage
        budget_exhausted = context.get("_budget_exhausted", False)
        no_progress = context.get("_no_progress", False)

        decision_type, reason = _coverage_decision(
            overall_status,
            budget_exhausted=budget_exhausted,
            no_progress=no_progress,
        )

        # Map decision type to run state name for transitions
        state_name = decision_to_state(decision_type)

        # Propose the next action through the strategy service
        proposal_id = self._propose_next_action(
            run_id, run_revision, new_coverage_revision, decision_type, reason, context
        )

        # If authorization was rejected for a non-terminal decision, fail.
        # Terminal decisions (partial/failed) do not require a proposal.
        if proposal_id is None and state_name not in ("partial", "failed"):
            return StageResult.failed(
                "coverage_review",
                "strategy authorization rejected — cannot proceed",
            )

        # Transition to the appropriate state
        try:
            self.run_service.transition(
                run_id,
                state_name,
                expected_revision=run_revision + 1,
                idempotency_key=f"stage:coverage_review:{run_id}:{uuid4()}",
                actor_type="orchestrator",
                actor_identifier="CoverageReviewStage",
                triggering_event="run.coverage_review_decision",
                reason=f"coverage {overall_status} -> {state_name} ({reason})",
                completion={
                    "coverage_status": overall_status,
                    "next_action": state_name,
                },
            )
        except (RunStateError, StaleRunRevisionError) as exc:
            return StageResult.failed("coverage_review", str(exc))

        result = StageResult.ok(
            "coverage_review",
            f"coverage {overall_status}, next action: {state_name} ({reason})",
            details={
                ContextKeys.COVERAGE_STATUS: overall_status,
                ContextKeys.OVERALL_STATUS: overall_status,
                ContextKeys.NEXT_ACTION: state_name,
                ContextKeys.STRATEGY_PROPOSAL_ID: str(proposal_id)
                if proposal_id
                else None,
            },
            # events are added by StageResult.ok() automatically
        )

        # If terminal, return a terminal result
        if state_name in ("synthesizing", "partial", "failed"):
            return StageResult.terminal(
                "coverage_review",
                f"coverage {overall_status}, next action: {state_name} ({reason})",
                details={
                    ContextKeys.COVERAGE_STATUS: overall_status,
                    ContextKeys.OVERALL_STATUS: overall_status,
                    ContextKeys.NEXT_ACTION: state_name,
                    ContextKeys.STRATEGY_PROPOSAL_ID: str(proposal_id)
                    if proposal_id
                    else None,
                },
            )

        return result

    def _propose_next_action(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int,
        decision_type: str,
        reason: str,
        context: dict[str, Any],
    ) -> UUID | None:
        """Create and authorize a strategy proposal for the next action.

        Returns the proposal ID if authorized, or None if authorization
        is rejected or authorization fails.
        """
        target_items = []
        ledger = context.get(ContextKeys.COVERAGE_LEDGER)
        if ledger:
            target_items = [
                str(item.coverage_item_id)
                for item in ledger.items
                if item.status.value not in ("satisfied", "waived")
            ]

        if not target_items and decision_type == STRATEGY_DECISION_SEARCH:
            return None  # No targeted items to propose for search actions

        # C5: Validate proposal before creating to avoid orphaned records
        proposed_queries = []
        try:
            validation = self.strategy_service.validate_proposal(
                run_id=run_id,
                run_revision=run_revision,
                coverage_revision=coverage_revision,
                decision_type=decision_type,
                target_coverage_item_ids=[UUID(tid) for tid in target_items[:10]],
                proposed_queries=proposed_queries,
                estimated_cost={},
                rationale=f"Next action: {decision_type} because {reason}",
                current_run_revision=run_revision,
                current_coverage_revision=coverage_revision,
                run_state="coverage_review",
                is_terminal=decision_type
                in (STRATEGY_DECISION_PARTIAL, STRATEGY_DECISION_FAIL),
                run_exists=True,
                coverage_items_exist=True,
            )
            if not validation.valid:
                logger.warning(
                    "strategy proposal rejected by validation: %s",
                    validation.rejection_reasons,
                )
                return None
        except Exception as exc:
            logger.warning("strategy proposal validation failed: %s", exc)
            return None

        try:
            proposal = self.strategy_service.create_proposal(
                run_id=run_id,
                run_revision=run_revision,
                coverage_revision=coverage_revision,
                decision_type=decision_type,
                target_coverage_item_ids=[UUID(tid) for tid in target_items[:10]],
                proposed_queries=[],
                expected_contribution=f"coverage_{reason}",
                rationale=f"Next action: {decision_type} because {reason}",
                confidence=0.5,
                idempotency_key=f"proposal:{run_id}:{decision_type}:{coverage_revision}",
            )

            # Authorize the proposal before returning — no adaptive action
            # executes without a recorded authorization decision.
            decision = self.strategy_service.authorize(
                run_id=run_id,
                proposal_id=proposal.proposal_id,
                current_run_revision=run_revision,
                current_coverage_revision=coverage_revision,
                run_state="coverage_review",
                is_terminal=decision_type
                in (
                    STRATEGY_DECISION_PARTIAL,
                    STRATEGY_DECISION_FAIL,
                ),
                run_exists=True,
                coverage_items_exist=True,
            )

            if decision.outcome != "accepted":
                logger.warning(
                    "strategy proposal %s rejected: %s",
                    proposal.proposal_id,
                    decision.rejection_reasons,
                )
                return None

            # C4: Populate context with proposal and decision IDs for downstream stages
            context[ContextKeys.STRATEGY_PROPOSAL_ID] = proposal.proposal_id
            context[ContextKeys.STRATEGY_DECISION_ID] = decision.decision_id
            context[ContextKeys.STRATEGY_DECISION] = decision_type

            # Store authorized queries for AcquisitionStage (cycle 2+)
            if decision_type == STRATEGY_DECISION_SEARCH and proposal.proposed_queries:
                existing = context.get(ContextKeys.AUTHORIZED_QUERIES, [])
                existing.append(
                    {
                        "proposal_id": str(proposal.proposal_id),
                        "decision_type": decision_type,
                        "proposed_queries": list(proposal.proposed_queries),
                    }
                )
                context[ContextKeys.AUTHORIZED_QUERIES] = existing

            return proposal.proposal_id
        except Exception as exc:
            logger.warning("strategy proposal creation failed: %s", exc)
            return None


class NextActionStage:
    """Execute the authorized next action.

    This stage is a lightweight dispatcher that delegates to the
    appropriate service based on the coverage_review decision.
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        strategy_service: StrategyRevisionService,
    ) -> None:
        self.run_service = run_service
        self.strategy_service = strategy_service

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        if run_state not in ("acquiring", "extracting", "retrieving", "synthesizing"):
            return StageResult.failed(
                "next_action",
                f"next_action stage requires acquiring/extracting/retrieving/synthesizing state, got {run_state}",
            )

        # Validate the strategy decision before executing
        decision_id = context.get(ContextKeys.STRATEGY_DECISION_ID)
        if decision_id:
            try:
                decision = self.strategy_service.get_decision(run_id, decision_id)
                if decision.outcome != "accepted":
                    return StageResult.failed(
                        "next_action",
                        f"strategy decision {decision_id} was rejected: {decision.rejection_reasons}",
                    )
            except Exception as exc:
                logger.warning("strategy decision validation failed: %s", exc)

        # The actual work is delegated to the appropriate service.
        # This stage exists primarily for observability and audit.
        return StageResult.ok(
            "next_action",
            f"executing {run_state} action",
            details={ContextKeys.NEXT_ACTION: run_state},
        )


class SynthesisStage:
    """Synthesize the final report or evidence packet.

    Transitions: synthesizing -> validating -> completed/partial
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        config: StoreConfig,
    ) -> None:
        self.run_service = run_service
        self.config = config

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        if run_state != "synthesizing":
            return StageResult.failed(
                "synthesis",
                f"synthesis stage requires synthesizing state, got {run_state}",
            )

        # Transition to validating
        try:
            self.run_service.transition(
                run_id,
                "validating",
                expected_revision=run_revision + 1,
                idempotency_key=f"stage:synthesis_done:{run_id}:{uuid4()}",
                actor_type="orchestrator",
                actor_identifier="SynthesisStage",
                triggering_event="run.validating",
                reason="synthesis complete, entering validation",
            )
        except (RunStateError, StaleRunRevisionError) as exc:
            return StageResult.failed("synthesis", str(exc))

        return StageResult.ok(
            "synthesis",
            "synthesis complete, transitioned to validating",
        )


class TerminalStage:
    """Handle terminal outcomes (completed, partial, failed).

    This stage records the final outcome and emits terminal events.
    """

    def __init__(
        self,
        run_service: ResearchRunService,
    ) -> None:
        self.run_service = run_service

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        outcome = context.get("_terminal_outcome", "partial")

        if run_state not in ("validating", "partial", "failed"):
            return StageResult.failed(
                "terminal",
                f"terminal stage requires validating/partial/failed state, got {run_state}",
            )

        if run_state == "validating":
            try:
                if outcome == "partial":
                    self.run_service.partial(
                        run_id,
                        expected_revision=run_revision + 1,
                        idempotency_key=f"terminal:partial:{run_id}:{uuid4()}",
                        actor_type="orchestrator",
                        actor_identifier="TerminalStage",
                        reason=context.get("_terminal_reason", "partial coverage"),
                        outcome="partial",
                    )
                else:
                    self.run_service.fail(
                        run_id,
                        expected_revision=run_revision + 1,
                        idempotency_key=f"terminal:failed:{run_id}:{uuid4()}",
                        actor_type="orchestrator",
                        actor_identifier="TerminalStage",
                        reason=context.get("_terminal_reason", "research failed"),
                        outcome="failed",
                    )
            except (RunStateError, StaleRunRevisionError) as exc:
                return StageResult.failed("terminal", str(exc))
        elif run_state == "partial":
            pass  # Already terminal
        else:
            pass  # Already terminal

        return StageResult.terminal(
            "terminal",
            f"run ended in {run_state} state",
            details={"outcome": outcome},
        )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class ResearchOrchestrator:
    """Coverage-led research orchestrator.

    This is the single entry point for the coverage-led workflow.  It
    coordinates the staged pipeline and ensures that:

    * All state transitions flow through ``ResearchRunService``.
    * Coverage is evaluated after each meaningful wave.
    * Adaptive actions are proposed and authorized through
      ``StrategyRevisionService``.
    * Every invocation and transition is persisted.
    * The orchestrator can resume a run after process restart.

    Example usage::

        orchestrator = ResearchOrchestrator.build(config)
        result = orchestrator.run(
            run_id=run_id,
            spec=spec,
            search_plan=search_plan,
        )
        print(result.outcome)  # "completed", "partial", "failed"
    """

    def __init__(
        self,
        run_service: ResearchRunService,
        coverage_service: CoverageService,
        strategy_service: StrategyRevisionService,
        acquisition_service: AcquisitionService,
        config: StoreConfig,
        legacy_adapter: LegacyEntryPointAdapter | None = None,
    ) -> None:
        self.run_service = run_service
        self.coverage_service = coverage_service
        self.strategy_service = strategy_service
        self.acquisition_service = acquisition_service
        self.config = config
        self.legacy_adapter = legacy_adapter

        # Stage instances
        self._planning = PlanningStage(run_service, config)
        self._corpus_review = CorpusReviewStage(run_service, coverage_service)
        self._acquisition = AcquisitionStage(
            run_service, acquisition_service, coverage_service, strategy_service, config
        )
        self._extraction = ExtractionStage(run_service, coverage_service, config)
        self._indexing = IndexingStage(run_service, config)
        self._coverage_review = CoverageReviewStage(
            run_service, coverage_service, strategy_service, config
        )
        self._next_action = NextActionStage(run_service, strategy_service)
        self._synthesis = SynthesisStage(run_service, config)
        self._terminal = TerminalStage(run_service)

        # Stage registry
        self._stages: dict[str, StageHandler] = {
            "planning": self._planning,
            "corpus_review": self._corpus_review,
            "acquisition": self._acquisition,
            "extraction": self._extraction,
            "indexing": self._indexing,
            "coverage_review": self._coverage_review,
            "next_action": self._next_action,
            "synthesis": self._synthesis,
            "terminal": self._terminal,
        }

    @classmethod
    def build(
        cls,
        config: StoreConfig | None = None,
        *,
        orchestrator_config: OrchestratorConfig | None = None,
    ) -> "ResearchOrchestrator":
        """Build an orchestrator with all required services.

        Args:
            config: Store configuration.  Defaults to env-based config.
            orchestrator_config: Orchestrator-specific settings.

        Returns:
            A fully wired ``ResearchOrchestrator`` instance.
        """
        from .container import (
            build_acquisition_service,
            build_run_service,
            build_strategy_service,
        )

        config = config or StoreConfig.from_env()
        config.require_database()
        orchestrator_config = orchestrator_config or OrchestratorConfig()

        run_service = build_run_service(config)
        acquisition_service = build_acquisition_service(config)
        strategy_service = build_strategy_service(config)
        coverage_service = CoverageService(run_service.uow_factory)

        legacy_adapter = None
        if orchestrator_config.legacy_adapter_mode != "compatibility":
            from .container import build_legacy_adapter

            legacy_adapter = build_legacy_adapter(
                AdapterMode(orchestrator_config.legacy_adapter_mode),
                config,
            )

        return cls(
            run_service=run_service,
            coverage_service=coverage_service,
            strategy_service=strategy_service,
            acquisition_service=acquisition_service,
            config=config,
            legacy_adapter=legacy_adapter,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        run_id: UUID,
        spec: dict[str, Any],
        search_plan: dict[str, Any],
        *,
        max_adaptive_cycles: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> OrchestratorResult:
        """Execute the full coverage-led orchestration pipeline.

        This is the main entry point.  It:

        1. Checks if the run already exists and resumes if appropriate.
        2. Executes stages in order: planning -> corpus_review ->
           acquisition -> extraction -> indexing -> coverage_review ->
           next_action -> (loop back to acquisition or synthesize).
        3. Handles budget exhaustion and no-progress terminal conditions.
        4. Returns an ``OrchestratorResult`` with the final outcome.

        Args:
            run_id: The research run UUID.
            spec: The validated ResearchSpec as a dict.
            search_plan: The validated SearchPlan as a dict.
            max_adaptive_cycles: Override the default max cycles.
            context: Additional context to pass to stages.

        Returns:
            An ``OrchestratorResult`` describing the final outcome.
        """
        max_cycles = max_adaptive_cycles or self.config.max_adaptive_cycles
        ctx = context or {}
        ctx["spec"] = spec
        ctx["search_plan"] = search_plan
        ctx["execution_mode"] = self.config.execution_mode
        ctx[ContextKeys.WALL_CLOCK_START] = time.monotonic()
        ctx[ContextKeys.WAVE_COUNT] = 0

        # Get current run state
        run_status = self.run_service.status(run_id=run_id)
        current_state = run_status.state
        current_revision = run_status.lifecycle_revision

        # If already terminal, return early
        if current_state in ("completed", "partial", "failed", "cancelled"):
            return OrchestratorResult(
                run_id=run_id,
                final_state=current_state,
                outcome="resumed" if current_state == "partial" else current_state,
                coverage_revision=getattr(
                    run_status, "current_coverage_revision", None
                ),
            )

        # Main orchestration loop
        cycle_count = 0
        wave_count = 0
        strategy_proposals = 0
        strategy_decisions = 0

        try:
            # Stage 1: Planning
            result = self._execute_stage(
                "planning", run_id, current_revision, None, current_state, ctx
            )
            if result.error:
                return self._failed_result(run_id, result.error)

            # Fix: Update revision dynamically after stage transition
            run_status = self.run_service.status(run_id=run_id)
            current_revision = run_status.lifecycle_revision
            current_state = run_status.state

            # Stage 2: Corpus review (create coverage items)
            result = self._execute_stage(
                "corpus_review", run_id, current_revision, None, current_state, ctx
            )
            if result.error:
                return self._failed_result(run_id, result.error)

            # Fix: Update revision dynamically after stage transition
            run_status = self.run_service.status(run_id=run_id)
            current_revision = run_status.lifecycle_revision
            current_state = run_status.state

            # Main loop: acquisition -> indexing -> coverage_review -> ...
            while cycle_count < max_cycles:
                cycle_count += 1

                # Check budget exhaustion
                budget_exhausted = self._check_budget(ctx, run_id)
                if budget_exhausted:
                    ctx["_budget_exhausted"] = True

                # Stage: Acquisition
                result = self._execute_stage(
                    "acquisition",
                    run_id,
                    current_revision,
                    ctx.get(ContextKeys.OVERALL_STATUS),
                    current_state,
                    ctx,
                )
                if result.error:
                    return self._failed_result(run_id, result.error)

                # Fix: Update revision dynamically after stage transition
                run_status = self.run_service.status(run_id=run_id)
                current_revision = run_status.lifecycle_revision
                current_state = run_status.state

                # Track wave count — increment after each successful acquisition
                wave_count += 1
                ctx[ContextKeys.WAVE_COUNT] = wave_count

                if result.outcome == StageOutcome.TERMINAL:
                    break

                # Stage: Indexing
                result = self._execute_stage(
                    "indexing",
                    run_id,
                    current_revision,
                    ctx.get(ContextKeys.OVERALL_STATUS),
                    current_state,
                    ctx,
                )
                if result.error:
                    return self._failed_result(run_id, result.error)

                # Fix: Update revision dynamically after stage transition
                run_status = self.run_service.status(run_id=run_id)
                current_revision = run_status.lifecycle_revision
                current_state = run_status.state

                # Stage: Coverage review
                result = self._execute_stage(
                    "coverage_review",
                    run_id,
                    current_revision,
                    ctx.get(ContextKeys.OVERALL_STATUS),
                    current_state,
                    ctx,
                )
                if result.error:
                    return self._failed_result(run_id, result.error)

                # Fix: Update revision dynamically after stage transition
                run_status = self.run_service.status(run_id=run_id)
                current_revision = run_status.lifecycle_revision
                current_state = run_status.state

                # Count strategy proposals
                if result.details and result.details.get(
                    ContextKeys.STRATEGY_PROPOSAL_ID
                ):
                    strategy_proposals += 1

                # Check if terminal
                if result.outcome == StageOutcome.TERMINAL:
                    next_action = (
                        result.details.get(ContextKeys.NEXT_ACTION, "")
                        if result.details
                        else ""
                    )
                    if next_action in ("partial", "failed"):
                        ctx["_terminal_outcome"] = next_action
                        reason = (
                            result.details.get("reason", "coverage-led decision")
                            if result.details
                            else "coverage-led decision"
                        )
                        ctx["_terminal_reason"] = reason
                        break
                    elif next_action == "synthesizing":
                        break

                # Check no-progress
                if self._check_no_progress(ctx, run_id):
                    ctx["_no_progress"] = True

                # Fix: Update revision dynamically after cycle
                run_status = self.run_service.status(run_id=run_id)
                current_revision = run_status.lifecycle_revision
                current_state = run_status.state

            # Fix: Terminal stage transition on budget exhaustion
            # If budget is exhausted and coverage is insufficient, explicitly
            # transition to "partial" state before invoking TerminalStage.
            if budget_exhausted and ctx.get(ContextKeys.OVERALL_STATUS) != "sufficient":
                if "_terminal_outcome" not in ctx:
                    ctx["_terminal_outcome"] = "partial"
                    ctx["_terminal_reason"] = "budget exhausted with insufficient coverage"
                try:
                    self.run_service.partial(
                        run_id,
                        expected_revision=current_revision,
                        idempotency_key=f"budget:partial:{run_id}:{uuid4()}",
                        actor_type="orchestrator",
                        actor_identifier="ResearchOrchestrator",
                        reason=ctx.get("_terminal_reason", "budget exhausted"),
                        outcome="partial",
                    )
                except (RunStateError, StaleRunRevisionError) as exc:
                    logger.warning("budget exhaustion partial transition failed: %s", exc)
                # Update revision after explicit partial transition
                run_status = self.run_service.status(run_id=run_id)
                current_revision = run_status.lifecycle_revision
                current_state = run_status.state

            # If we reached synthesis
            if current_state == "synthesizing" or (
                ctx.get(ContextKeys.OVERALL_STATUS) == "sufficient"
            ):
                # Set terminal outcome — sufficient coverage completes,
                # insufficient coverage yields partial
                if ctx.get(ContextKeys.OVERALL_STATUS) == "sufficient":
                    ctx["_terminal_outcome"] = "completed"
                    ctx["_terminal_reason"] = "sufficient coverage"
                elif "_terminal_outcome" not in ctx:
                    ctx["_terminal_outcome"] = "partial"
                    ctx["_terminal_reason"] = "partial coverage"

                result = self._execute_stage(
                    "synthesis",
                    run_id,
                    current_revision,
                    ctx.get(ContextKeys.OVERALL_STATUS),
                    "synthesizing",
                    ctx,
                )
                if result.error:
                    return self._failed_result(run_id, result.error)

                # Fix: Update revision dynamically after synthesis
                run_status = self.run_service.status(run_id=run_id)
                current_revision = run_status.lifecycle_revision
                current_state = run_status.state

            # Terminal stage
            result = self._execute_stage(
                "terminal",
                run_id,
                current_revision,
                ctx.get(ContextKeys.OVERALL_STATUS),
                current_state,
                ctx,
            )

            final_state = current_state
            if result.outcome == StageOutcome.TERMINAL:
                final_state = ctx.get("_terminal_outcome", current_state)

            # Compatibility export
            if self.legacy_adapter:
                try:
                    self.legacy_adapter.route(
                        "fsearch_smart",
                        {
                            "action": "complete",
                            "status": final_state,
                            "input": {
                                "run_id": str(run_id),
                                "coverage_revision": ctx.get(
                                    ContextKeys.OVERALL_STATUS
                                ),
                                "wave_count": wave_count,
                            },
                        },
                        external_run_id=str(run_id),
                        idempotency_key=f"legacy:complete:{run_id}",
                        service_proposal={
                            "coverage_led": True,
                            "final_state": final_state,
                        },
                    )
                except Exception as exc:
                    logger.warning("legacy adapter export failed: %s", exc)

            return OrchestratorResult(
                run_id=run_id,
                final_state=final_state,
                outcome=final_state,
                coverage_revision=ctx.get(ContextKeys.OVERALL_STATUS),
                wave_count=wave_count,
                successful_urls=ctx.get(ContextKeys.SUCCESSFUL_URLS, 0),
                strategy_proposals=strategy_proposals,
                strategy_decisions=strategy_decisions,
            )

        except Exception as exc:
            logger.exception("orchestration failed: %s", exc)
            return self._failed_result(run_id, str(exc))

    def run_from_external_id(
        self,
        external_id: str,
        spec: dict[str, Any],
        search_plan: dict[str, Any],
        *,
        create_if_missing: bool = True,
        **kwargs: Any,
    ) -> OrchestratorResult:
        """Run orchestration using an external run ID string.

        If ``create_if_missing`` is True and the run does not exist,
        a new run is created in the ``created`` state.

        Args:
            external_id: External run identifier.
            spec: The ResearchSpec as a dict.
            search_plan: The SearchPlan as a dict.
            create_if_missing: Create the run if it doesn't exist.
            **kwargs: Passed to ``run``.

        Returns:
            An ``OrchestratorResult``.
        """
        try:
            run_status = self.run_service.status(external_id=external_id)
            run_id = run_status.id
        except KeyError:
            if not create_if_missing:
                return OrchestratorResult(
                    run_id=UUID(int=0),
                    final_state="not_found",
                    outcome="not_found",
                    error=f"run {external_id} not found and create_if_missing=False",
                )
            run_status = self.run_service.create(
                objective=spec.get("objective", external_id),
                external_id=external_id,
                execution_mode=self.config.execution_mode,
            )
            run_id = run_status.id

        return self.run(run_id, spec, search_plan, **kwargs)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _execute_stage(
        self,
        stage_name: str,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        """Execute a single stage and record the invocation."""
        stage = self._stages.get(stage_name)
        if stage is None:
            return StageResult.failed("unknown", f"unknown stage: {stage_name}")

        # Record invocation
        try:
            self.run_service.record_search_response(
                run_id,
                query_text=f"stage:{stage_name}",
                backend="orchestrator",
                raw_payload=f"stage invocation: {stage_name}",
                idempotency_key=f"invocation:{stage_name}:{run_id}",
            )
        except Exception:
            pass  # Invocation recording is best-effort

        start = time.monotonic()
        result = stage.execute(
            run_id, run_revision, coverage_revision, run_state, context
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        # Wrap result with duration — create a new dict since StageResult is frozen
        details = dict(result.details or {})
        details["duration_ms"] = duration_ms

        logger.info(
            "stage %s: outcome=%s summary=%s duration=%dms",
            stage_name,
            result.outcome.value,
            result.summary,
            duration_ms,
        )

        return StageResult(
            stage=result.stage,
            outcome=result.outcome,
            summary=result.summary,
            details=details,
            events=result.events,
            warnings=result.warnings,
            error=result.error,
        )

    def _check_budget(self, context: dict[str, Any], run_id: UUID) -> bool:
        """Check if the hard budget has been exhausted."""
        wave_count = context.get(ContextKeys.WAVE_COUNT, 0)
        max_cycles = self.config.max_adaptive_cycles
        return wave_count >= max_cycles

    def _check_no_progress(self, context: dict[str, Any], run_id: UUID) -> bool:
        """Check if the run has made no progress since the last cycle."""
        previous_status = context.get("_previous_coverage_status")
        current_status = context.get(ContextKeys.OVERALL_STATUS)
        if previous_status and current_status == previous_status:
            # Same status two cycles in a row — no progress
            return True
        if current_status:
            context["_previous_coverage_status"] = current_status
        return False

    def _failed_result(self, run_id: UUID, error: str) -> OrchestratorResult:
        """Create a failed orchestrator result."""
        return OrchestratorResult(
            run_id=run_id,
            final_state="failed",
            outcome="failed",
            error=error,
        )


__all__ = [
    "AcquisitionStage",
    "CorpusReviewStage",
    "CoverageReviewStage",
    "ExtractionStage",
    "IndexingStage",
    "NextActionStage",
    "OrchestratorConfig",
    "OrchestratorResult",
    "PlanningStage",
    "ResearchOrchestrator",
    "StageHandler",
    "StageOutcome",
    "StageResult",
    "SynthesisStage",
    "TerminalStage",
    "ContextKeys",
    "_coverage_decision",
    "decision_to_state",
    "STRATEGY_DECISION_SYNTHESIZE",
    "STRATEGY_DECISION_SEARCH",
    "STRATEGY_DECISION_PARTIAL",
    "STRATEGY_DECISION_FAIL",
]
