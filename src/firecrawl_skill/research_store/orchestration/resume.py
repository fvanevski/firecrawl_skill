"""Canonical resume orchestration lifecycle.

This module contains the single authoritative implementation of the
resume control flow. ``ResumableResearchOrchestrator.run`` delegates to
``run_resume`` to avoid duplicating the logic.

State queries are routed through ``ResumeStatePort`` so that no raw SQL
lives in this package. Deterministic reconstruction helpers live in
``resume_support`` and have no dependency on the compatibility facade.
"""

from __future__ import annotations

import logging
import time

from ..orchestrator import OrchestratorResult, ResearchOrchestrator
from ..run_service import RunStateError, StaleRunRevisionError
from ..stages import ContextKeys
from .commands import RunResearchCommand
from .ports import ResumeStatePort
from .resume_support import (
    PLANNING_STATES,
    TERMINAL_STATES,
    SmartResumeError,
    coverage_context,
    replay_extraction_inputs,
)

logger = logging.getLogger(__name__)


def run_resume(
    orchestrator: ResearchOrchestrator,
    command: RunResearchCommand,
    *,
    state_port: ResumeStatePort,
) -> OrchestratorResult:
    """Execute the resume orchestration pipeline.

    This is the canonical resume lifecycle. ``ResumableResearchOrchestrator.run``
    is a thin delegation to this function.

    Args:
        orchestrator: The orchestrator instance.
        command: The ``RunResearchCommand`` carrying the run identity, spec,
            search plan, cycle bound, and stage context.
        state_port: Read-only port for state queries.

    Returns:
        An ``OrchestratorResult`` describing the final outcome.
    """
    run_id = command.run_id
    spec = command.spec
    search_plan = command.search_plan
    max_adaptive_cycles = command.max_adaptive_cycles
    context = command.context
    max_cycles = (
        max_adaptive_cycles or orchestrator.orchestrator_config.max_adaptive_cycles
    )
    ctx = dict(context or {})
    ctx.update(
        {
            "spec": spec,
            "search_plan": search_plan,
            "execution_mode": orchestrator.orchestrator_config.execution_mode,
            "_max_adaptive_cycles": max_cycles,
        }
    )
    ctx.setdefault(ContextKeys.WALL_CLOCK_START, time.monotonic())
    state, revision = orchestrator._refresh(run_id)
    counts = state_port.counts(run_id)
    ctx.setdefault(ContextKeys.WAVE_COUNT, counts.waves)
    ctx.setdefault(ContextKeys.EXTRACTION_ATTEMPTS, counts.attempts)
    ctx.setdefault(ContextKeys.SUCCESSFUL_URLS, counts.assets)
    if state not in PLANNING_STATES and state not in TERMINAL_STATES:
        ctx.update(coverage_context(orchestrator, run_id))
    ctx.setdefault(
        ContextKeys.AUTHORIZED_QUERIES, state_port.authorized_queries(run_id)
    )
    coverage_revision = int(ctx.get("coverage_revision") or 0) or None

    if state in TERMINAL_STATES:
        return OrchestratorResult(
            run_id=run_id,
            final_state=state,
            outcome="resumed",
            coverage_revision=coverage_revision,
            wave_count=counts.waves,
            successful_urls=counts.assets,
        )

    try:
        if state == "created":
            result = orchestrator._execute_stage(
                "planning", run_id, revision, coverage_revision, state, ctx
            )
            if result.error:
                return orchestrator._failed_result(run_id, result.error)
            state, revision = orchestrator._refresh(run_id)
            checkpoint = orchestrator._checkpoint(run_id, ctx, state)
            if checkpoint:
                return checkpoint
        elif state == "planning":
            orchestrator.run_service.transition(
                run_id,
                "corpus_review",
                expected_revision=revision,
                idempotency_key=f"resume:planning-complete:{run_id}",
                actor_type="orchestrator",
                actor_identifier="ResumableResearchOrchestrator",
                triggering_event="run.corpus_review",
                reason="resume from persisted planning tuple",
            )
            state, revision = orchestrator._refresh(run_id)

        if state == "corpus_review":
            result = orchestrator._execute_stage(
                "corpus_review", run_id, revision, coverage_revision, state, ctx
            )
            if result.error:
                return orchestrator._failed_result(run_id, result.error)
            state, revision = orchestrator._refresh(run_id)
            ctx.update(coverage_context(orchestrator, run_id))
            coverage_revision = int(ctx.get("coverage_revision") or 1)
            checkpoint = orchestrator._checkpoint(run_id, ctx, state)
            if checkpoint:
                return checkpoint

        iterations = 0
        while state not in TERMINAL_STATES:
            iterations += 1
            if iterations > max(12, max_cycles * 6):
                raise SmartResumeError("resume loop exceeded its safety bound")

            if state == "acquiring":
                if int(ctx.get(ContextKeys.WAVE_COUNT, 0)) >= max_cycles:
                    ctx["_budget_exhausted"] = True
                    orchestrator.run_service.transition(
                        run_id,
                        "coverage_review",
                        expected_revision=revision,
                        idempotency_key=f"resume:budget-review:{run_id}:{revision}",
                        actor_type="orchestrator",
                        actor_identifier="ResumableResearchOrchestrator",
                        triggering_event="run.coverage_review",
                        reason="adaptive-cycle budget exhausted",
                    )
                else:
                    result = orchestrator._execute_stage(
                        "acquisition",
                        run_id,
                        revision,
                        coverage_revision,
                        state,
                        ctx,
                    )
                    if result.error:
                        return orchestrator._failed_result(run_id, result.error)
                    ctx[ContextKeys.WAVE_COUNT] = (
                        int(ctx.get(ContextKeys.WAVE_COUNT, 0)) + 1
                    )
                state, revision = orchestrator._refresh(run_id)
                checkpoint = orchestrator._checkpoint(run_id, ctx, state)
                if checkpoint:
                    return checkpoint
                continue

            if state == "extracting":
                inputs = list(ctx.get("raw_ingest_requests") or [])
                if not inputs:
                    inputs = replay_extraction_inputs(
                        orchestrator,
                        run_id,
                        ctx,
                        completed_candidates=state_port.completed_candidates(run_id),
                    )
                if inputs:
                    result = orchestrator._execute_stage(
                        "extraction",
                        run_id,
                        revision,
                        coverage_revision,
                        state,
                        ctx,
                    )
                    if result.error:
                        return orchestrator._failed_result(run_id, result.error)
                else:
                    restored = state_port.assets(run_id)
                    next_state = "indexing" if restored else "coverage_review"
                    ctx["extracted_assets"] = restored
                    orchestrator.run_service.transition(
                        run_id,
                        next_state,
                        expected_revision=revision,
                        idempotency_key=f"resume:extraction:{run_id}:{next_state}",
                        actor_type="orchestrator",
                        actor_identifier="ResumableResearchOrchestrator",
                        triggering_event=f"run.{next_state}",
                        reason="resume found no unprocessed candidates",
                    )
                state, revision = orchestrator._refresh(run_id)
                checkpoint = orchestrator._checkpoint(run_id, ctx, state)
                if checkpoint:
                    return checkpoint
                continue

            if state == "indexing":
                ctx["extracted_assets"] = state_port.assets(run_id)
                if not ctx["extracted_assets"]:
                    raise SmartResumeError("indexing state has no persisted chunks")
                result = orchestrator._execute_stage(
                    "indexing",
                    run_id,
                    revision,
                    coverage_revision,
                    state,
                    ctx,
                )
                if result.error:
                    return orchestrator._failed_result(run_id, result.error)
                state, revision = orchestrator._refresh(run_id)
                result = orchestrator._execute_stage(
                    "evidence_preparation",
                    run_id,
                    revision,
                    coverage_revision,
                    state,
                    ctx,
                )
                if result.error:
                    return orchestrator._failed_result(run_id, result.error)
                state, revision = orchestrator._refresh(run_id)
                checkpoint = orchestrator._checkpoint(run_id, ctx, state)
                if checkpoint:
                    return checkpoint
                continue

            if state == "retrieving":
                orchestrator.run_service.transition(
                    run_id,
                    "coverage_review",
                    expected_revision=revision,
                    idempotency_key=f"resume:retrieval:{run_id}:{revision}",
                    actor_type="orchestrator",
                    actor_identifier="ResumableResearchOrchestrator",
                    triggering_event="run.coverage_review",
                    reason="resume retrieval from authoritative corpus",
                )
                state, revision = orchestrator._refresh(run_id)
                continue

            if state == "coverage_review":
                ctx.update(coverage_context(orchestrator, run_id))
                coverage_revision = int(ctx.get("coverage_revision") or 1)
                if int(ctx.get(ContextKeys.WAVE_COUNT, 0)) >= max_cycles:
                    ctx["_budget_exhausted"] = True
                result = orchestrator._execute_stage(
                    "coverage_review",
                    run_id,
                    revision,
                    coverage_revision,
                    state,
                    ctx,
                )
                if result.error:
                    return orchestrator._failed_result(run_id, result.error)
                state, revision = orchestrator._refresh(run_id)
                checkpoint = orchestrator._checkpoint(run_id, ctx, state)
                if checkpoint:
                    return checkpoint
                continue

            if state == "synthesizing":
                ctx["evidence_packet_revision"] = state_port.packet_revision(run_id)
                result = orchestrator._execute_stage(
                    "synthesis",
                    run_id,
                    revision,
                    coverage_revision,
                    state,
                    ctx,
                )
                if result.error:
                    return orchestrator._failed_result(run_id, result.error)
                state, revision = orchestrator._refresh(run_id)
                checkpoint = orchestrator._checkpoint(run_id, ctx, state)
                if checkpoint:
                    return checkpoint
                continue

            if state == "validating":
                ctx.update(coverage_context(orchestrator, run_id))
                ctx["_terminal_outcome"] = (
                    "completed"
                    if ctx.get(ContextKeys.OVERALL_STATUS) == "sufficient"
                    else "partial"
                )
                ctx["_terminal_reason"] = "resumed validation checkpoint"
                result = orchestrator._execute_stage(
                    "terminal",
                    run_id,
                    revision,
                    coverage_revision,
                    state,
                    ctx,
                )
                if result.error:
                    return orchestrator._failed_result(run_id, result.error)
                state, revision = orchestrator._refresh(run_id)
                continue

            raise SmartResumeError(f"unsupported persisted state: {state}")

        counts = state_port.counts(run_id)
        return OrchestratorResult(
            run_id=run_id,
            final_state=state,
            outcome=state,
            coverage_revision=coverage_revision,
            wave_count=counts.waves,
            successful_urls=counts.assets,
        )
    except (RunStateError, StaleRunRevisionError, SmartResumeError) as exc:
        logger.error("smart-run resume failed: %s", exc)
        return orchestrator._failed_result(run_id, str(exc))
