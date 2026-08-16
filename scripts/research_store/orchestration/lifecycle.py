"""Canonical fresh-run orchestration lifecycle.

This module contains the single authoritative implementation of the
fresh-run control flow.  ``ResearchOrchestrator.run`` delegates to
``run_research`` to avoid duplicating the logic.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID, uuid4

from ..orchestrator import (
    ContextKeys,
    OrchestratorResult,
    ResearchOrchestrator,
)
from ..run_service import RunStateError, StaleRunRevisionError
from ..stages import StageOutcome
from ..terminal_decision import TerminalDecisionOutcome

logger = logging.getLogger(__name__)


def run_research(
    orchestrator: ResearchOrchestrator,
    run_id: UUID,
    spec: dict[str, Any],
    search_plan: dict[str, Any],
    *,
    max_adaptive_cycles: int | None = None,
    context: dict[str, Any] | None = None,
) -> OrchestratorResult:
    """Execute the full coverage-led orchestration pipeline.

    This is the canonical fresh-run lifecycle.  ``ResearchOrchestrator.run``
    is a thin delegation to this function.

    Args:
        orchestrator: The orchestrator instance (provides services + stages).
        run_id: The research run UUID.
        spec: The validated ResearchSpec as a dict.
        search_plan: The validated SearchPlan as a dict.
        max_adaptive_cycles: Override the default max cycles.
        context: Additional context to pass to stages.

    Returns:
        An ``OrchestratorResult`` describing the final outcome.
    """
    max_cycles = (
        max_adaptive_cycles or orchestrator.orchestrator_config.max_adaptive_cycles
    )
    ctx = context or {}
    ctx["spec"] = spec
    ctx["search_plan"] = search_plan
    ctx["execution_mode"] = orchestrator.orchestrator_config.execution_mode
    ctx["_max_adaptive_cycles"] = max_cycles
    ctx[ContextKeys.WALL_CLOCK_START] = time.monotonic()
    ctx[ContextKeys.WAVE_COUNT] = 0

    # Get current run state
    run_status = orchestrator.run_service.status(run_id=run_id)
    current_state = run_status.state
    current_revision = run_status.lifecycle_revision

    # If already terminal, return early
    if current_state in ("completed", "partial", "failed", "cancelled"):
        return OrchestratorResult(
            run_id=run_id,
            final_state=current_state,
            outcome="resumed" if current_state == "partial" else current_state,
            coverage_revision=getattr(run_status, "current_coverage_revision", None),
        )

    # Main orchestration loop
    cycle_count = 0
    wave_count = 0
    strategy_proposals = 0
    strategy_decisions = 0
    coverage_revision_num = 0  # Track coverage revision across cycles
    # Accumulators for terminal-decision policy signals
    _repeated_extraction_failures = 0
    _repeated_retrieval_count = 0
    _unsatisfiable_source = False

    try:
        # Stage 1: Planning
        result = orchestrator._execute_stage(
            "planning", run_id, current_revision, None, current_state, ctx
        )
        if result.error:
            return orchestrator._failed_result(run_id, result.error)

        # Fix: Update revision dynamically after stage transition
        run_status = orchestrator.run_service.status(run_id=run_id)
        current_revision = run_status.lifecycle_revision
        current_state = run_status.state

        # Stage 2: Corpus review (create coverage items)
        result = orchestrator._execute_stage(
            "corpus_review", run_id, current_revision, None, current_state, ctx
        )
        if result.error:
            return orchestrator._failed_result(run_id, result.error)

        # Fix: Update revision dynamically after stage transition
        run_status = orchestrator.run_service.status(run_id=run_id)
        current_revision = run_status.lifecycle_revision
        current_state = run_status.state
        # Initialize coverage revision from run status (CorpusReviewStage creates revision 1)
        coverage_revision_num = getattr(run_status, "current_coverage_revision", 0) or 1

        # Main loop: acquisition -> extraction -> indexing -> coverage_review -> ...
        while cycle_count < max_cycles:
            cycle_count += 1

            # Stage: Acquisition
            result = orchestrator._execute_stage(
                "acquisition",
                run_id,
                current_revision,
                coverage_revision_num,
                current_state,
                ctx,
            )
            if result.error:
                return orchestrator._failed_result(run_id, result.error)

            # Fix: Update revision dynamically after stage transition
            run_status = orchestrator.run_service.status(run_id=run_id)
            current_revision = run_status.lifecycle_revision
            current_state = run_status.state

            # Track wave count — increment after each successful acquisition
            wave_count += 1
            ctx[ContextKeys.WAVE_COUNT] = wave_count

            # Track new candidates from acquisition stage
            if result.details and result.details.get(ContextKeys.CANDIDATE_COUNT):
                ctx["_new_candidate_count"] = result.details.get(
                    ContextKeys.CANDIDATE_COUNT, 0
                )

            # Evaluate budget exhaustion after wave_count is updated so that
            # CoverageReviewStage receives _budget_exhausted = True on the final allowed wave.
            budget_exhausted = orchestrator._check_budget(ctx, run_id)
            if budget_exhausted:
                ctx["_budget_exhausted"] = True

            if result.outcome == StageOutcome.TERMINAL:
                break

            # Stage: Extraction (only when acquisition found candidates)
            if current_state == "extracting":
                result = orchestrator._execute_stage(
                    "extraction",
                    run_id,
                    current_revision,
                    coverage_revision_num,
                    current_state,
                    ctx,
                )
                if result.error:
                    return orchestrator._failed_result(run_id, result.error)
                run_status = orchestrator.run_service.status(run_id=run_id)
                current_revision = run_status.lifecycle_revision
                current_state = run_status.state

            # Stage: Indexing (only after successful extraction)
            if current_state == "indexing":
                result = orchestrator._execute_stage(
                    "indexing",
                    run_id,
                    current_revision,
                    coverage_revision_num,
                    current_state,
                    ctx,
                )
                if result.error:
                    return orchestrator._failed_result(run_id, result.error)
                indexing_result = result

                # Fix: Update revision dynamically after stage transition
                run_status = orchestrator.run_service.status(run_id=run_id)
                current_revision = run_status.lifecycle_revision
                current_state = run_status.state

                result = orchestrator._execute_stage(
                    "evidence_preparation",
                    run_id,
                    current_revision,
                    coverage_revision_num,
                    current_state,
                    ctx,
                )
                if result.error:
                    return orchestrator._failed_result(run_id, result.error)

                # Track new assets from indexing stage (successful URLs)
                if indexing_result.details and indexing_result.details.get(
                    ContextKeys.SUCCESSFUL_URLS
                ):
                    ctx["_new_asset_count"] = indexing_result.details.get(
                        ContextKeys.SUCCESSFUL_URLS, 0
                    )

                # Accumulate extraction failure and retrieval counts from indexing
                if indexing_result.details:
                    attempts = indexing_result.details.get(
                        ContextKeys.EXTRACTION_ATTEMPTS, 0
                    )
                    success = indexing_result.details.get(
                        ContextKeys.EXTRACTION_SUCCESS_COUNT, 0
                    )
                    if isinstance(attempts, int) and isinstance(success, int):
                        _repeated_extraction_failures = max(
                            _repeated_extraction_failures,
                            attempts - success,
                        )
                    retrieval = indexing_result.details.get(
                        ContextKeys.RETRIEVAL_COUNT, 0
                    )
                    if isinstance(retrieval, int):
                        _repeated_retrieval_count = max(
                            _repeated_retrieval_count, retrieval
                        )

            # Stage: Coverage review
            result = orchestrator._execute_stage(
                "coverage_review",
                run_id,
                current_revision,
                coverage_revision_num,
                current_state,
                ctx,
            )
            if result.error:
                return orchestrator._failed_result(run_id, result.error)

            # Fix: Update revision dynamically after stage transition
            run_status = orchestrator.run_service.status(run_id=run_id)
            current_revision = run_status.lifecycle_revision
            current_state = run_status.state
            # Update coverage revision from run status
            coverage_revision_num = (
                getattr(run_status, "current_coverage_revision", coverage_revision_num)
                or coverage_revision_num
            )

            # Track strategy proposals
            if result.details and result.details.get(ContextKeys.STRATEGY_PROPOSAL_ID):
                strategy_proposals += 1

            # Track equivalent proposals (from strategy proposals in this cycle)
            equivalent_proposals_in_cycle = result.details.get(
                ContextKeys.STRATEGY_PROPOSAL_ID
            )
            if equivalent_proposals_in_cycle:
                ctx["_equivalent_proposal_count"] = (
                    ctx.get("_equivalent_proposal_count", 0) + 1
                )

            # Track changed coverage items from coverage review
            if result.details and result.details.get(ContextKeys.COVERAGE_LEDGER):
                ledger = result.details.get(ContextKeys.COVERAGE_LEDGER)
                if ledger and hasattr(ledger, "items"):
                    ctx["_changed_coverage_count"] = len(ledger.items)

            # Wire _unsatisfiable_source from overall coverage status
            if ctx.get(ContextKeys.OVERALL_STATUS) == "blocked":
                _unsatisfiable_source = True

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
            if orchestrator._check_no_progress(ctx, run_id):
                ctx["_no_progress"] = True

            # Wire accumulators into context for terminal-decision policy
            ctx["_strategy_revision_count"] = strategy_proposals
            ctx["_repeated_extraction_failures"] = _repeated_extraction_failures
            ctx["_repeated_retrieval_count"] = _repeated_retrieval_count
            ctx["_unsatisfiable_source"] = _unsatisfiable_source

            # Evaluate terminal decision policy
            terminal_decision = orchestrator._evaluate_terminal_decision(
                ctx, run_id, current_revision, coverage_revision_num
            )
            if terminal_decision is not None:
                # Policy returned a terminal decision — atomically persist
                # the decision and apply the lifecycle transition in a
                # single PostgreSQL transaction.
                TERMINAL_STATES = ("completed", "partial", "failed", "cancelled")
                if current_state not in TERMINAL_STATES:
                    try:
                        idempotency_key = f"terminal:{run_id}:r{current_revision}:c{coverage_revision_num}"
                        if terminal_decision.outcome == TerminalDecisionOutcome.FAILED:
                            next_state = "failed"
                        elif terminal_decision.outcome in (
                            TerminalDecisionOutcome.PARTIAL,
                            TerminalDecisionOutcome.BLOCKED,
                        ):
                            next_state = "partial"
                        else:
                            next_state = "completed"

                        result = orchestrator.run_service.commit_terminal_decision(
                            run_id,
                            decision_id=terminal_decision.decision_id,
                            run_revision=current_revision,
                            coverage_revision=coverage_revision_num,
                            outcome=terminal_decision.outcome.value,
                            no_progress_signals=tuple(
                                s.value for s in terminal_decision.no_progress_signals
                            ),
                            unresolved_gap=terminal_decision.unresolved_gap,
                            policy_version=terminal_decision.policy_version,
                            idempotency_key=idempotency_key,
                            created_at=terminal_decision.created_at,
                            next_state=next_state,
                            expected_revision=current_revision,
                            actor_type="orchestrator",
                            actor_identifier="ResearchOrchestrator",
                            reason=ctx.get(
                                "_terminal_reason",
                                "terminal decision policy triggered",
                            ),
                        )

                        # Update context after successful atomic commit
                        ctx["_terminal_signals"] = [
                            s.value for s in terminal_decision.no_progress_signals
                        ]
                        ctx["_terminal_outcome"] = terminal_decision.outcome.value
                        ctx["_terminal_reason"] = (
                            terminal_decision.unresolved_gap
                            or ctx.get("_terminal_reason", "")
                        )

                        run_status = orchestrator.run_service.status(run_id=run_id)
                        current_revision = run_status.lifecycle_revision
                        current_state = run_status.state
                    except (RunStateError, StaleRunRevisionError) as exc:
                        logger.warning(
                            "terminal decision atomic commit failed: %s",
                            exc,
                        )
                        raise
                break

            # Fix: Update revision dynamically after cycle
            run_status = orchestrator.run_service.status(run_id=run_id)
            current_revision = run_status.lifecycle_revision
            current_state = run_status.state

        # Fix: Terminal stage transition on budget exhaustion
        # If budget is exhausted and coverage is insufficient, explicitly
        # transition to "partial" state before invoking TerminalStage (unless already in a terminal state).
        TERMINAL_STATES = ("completed", "partial", "failed", "cancelled")
        if (
            budget_exhausted
            and current_state not in TERMINAL_STATES
            and ctx.get(ContextKeys.OVERALL_STATUS) != "sufficient"
        ):
            if "_terminal_outcome" not in ctx:
                ctx["_terminal_outcome"] = "partial"
                ctx["_terminal_reason"] = "budget exhausted with insufficient coverage"
            try:
                orchestrator.run_service.partial(
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
            run_status = orchestrator.run_service.status(run_id=run_id)
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

            result = orchestrator._execute_stage(
                "synthesis",
                run_id,
                current_revision,
                coverage_revision_num,
                "synthesizing",
                ctx,
            )
            if result.error:
                return orchestrator._failed_result(run_id, result.error)

            # Fix: Update revision dynamically after synthesis
            run_status = orchestrator.run_service.status(run_id=run_id)
            current_revision = run_status.lifecycle_revision
            current_state = run_status.state

        # Terminal stage
        result = orchestrator._execute_stage(
            "terminal",
            run_id,
            current_revision,
            coverage_revision_num,
            current_state,
            ctx,
        )

        final_state = current_state
        if result.outcome == StageOutcome.TERMINAL:
            final_state = ctx.get("_terminal_outcome", current_state)

        return OrchestratorResult(
            run_id=run_id,
            final_state=final_state,
            outcome=final_state,
            coverage_revision=coverage_revision_num,
            wave_count=wave_count,
            successful_urls=ctx.get(ContextKeys.SUCCESSFUL_URLS, 0),
            strategy_proposals=strategy_proposals,
            strategy_decisions=strategy_decisions,
        )

    except Exception as exc:
        logger.exception("orchestration failed: %s", exc)  # noqa: TRY401
        return orchestrator._failed_result(run_id, str(exc))
