"""Canonical resume orchestration lifecycle.

This module contains the single authoritative implementation of the
resume control flow.  ``ResumableResearchOrchestrator.run`` delegates to
``run_resume`` to avoid duplicating the logic.

State queries are routed through ``ResumeStatePort`` so that no raw SQL
lives in this package.  The default port adapter wraps the existing
authoritative helpers in ``smart_orchestrator``.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from ..orchestrator import OrchestratorResult, ResearchOrchestrator
from ..run_service import RunStateError, StaleRunRevisionError
from ..smart_orchestrator import (
    PLANNING_STATES,
    TERMINAL_STATES,
    SmartResumeError,
    _assets,
    _authorized_queries,
    _counts,
    _coverage_context,
    _packet_revision,
    _replay_extraction_inputs,
)
from ..stages import ContextKeys
from .ports import ResumeStatePort

logger = logging.getLogger(__name__)


class _DefaultResumeStatePort:
    """Adapts the existing smart_orchestrator helpers to ResumeStatePort."""

    def __init__(self, orchestrator: ResearchOrchestrator) -> None:
        self._orchestrator = orchestrator

    def counts(self, run_id: UUID) -> dict[str, int]:
        return _counts(self._orchestrator, run_id)

    def authorized_queries(self, run_id: UUID) -> list[dict[str, Any]]:
        return _authorized_queries(self._orchestrator, run_id)

    def completed_candidates(self, run_id: UUID) -> set[str]:
        from ..smart_orchestrator import _completed_candidates

        return _completed_candidates(self._orchestrator, run_id)

    def extraction_inputs(
        self, run_id: UUID, context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return _replay_extraction_inputs(self._orchestrator, run_id, context)

    def assets(self, run_id: UUID) -> list[dict[str, Any]]:
        return _assets(self._orchestrator, run_id)

    def packet_revision(self, run_id: UUID) -> int:
        return _packet_revision(self._orchestrator, run_id)


def run_resume(
    orchestrator: ResearchOrchestrator,
    run_id: UUID,
    spec: dict[str, Any],
    search_plan: dict[str, Any],
    *,
    max_adaptive_cycles: int | None = None,
    context: dict[str, Any] | None = None,
    state_port: ResumeStatePort | None = None,
) -> OrchestratorResult:
    """Execute the resume orchestration pipeline.

    This is the canonical resume lifecycle.  ``ResumableResearchOrchestrator.run``
    is a thin delegation to this function.

    Args:
        orchestrator: The orchestrator instance.
        run_id: The research run UUID.
        spec: The validated ResearchSpec as a dict.
        search_plan: The validated SearchPlan as a dict.
        max_adaptive_cycles: Override the default max cycles.
        context: Additional context to pass to stages.
        state_port: Port for state queries.  Defaults to the built-in adapter.

    Returns:
        An ``OrchestratorResult`` describing the final outcome.
    """
    if state_port is None:
        state_port = _DefaultResumeStatePort(orchestrator)

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
    ctx.setdefault(ContextKeys.WAVE_COUNT, counts["waves"])
    ctx.setdefault(ContextKeys.EXTRACTION_ATTEMPTS, counts["attempts"])
    ctx.setdefault(ContextKeys.SUCCESSFUL_URLS, counts["assets"])
    if state not in PLANNING_STATES and state not in TERMINAL_STATES:
        ctx.update(_coverage_context(orchestrator, run_id))
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
            wave_count=counts["waves"],
            successful_urls=counts["assets"],
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
            ctx.update(_coverage_context(orchestrator, run_id))
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
                    inputs = state_port.extraction_inputs(run_id, ctx)
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
                ctx.update(_coverage_context(orchestrator, run_id))
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
                ctx.update(_coverage_context(orchestrator, run_id))
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
            wave_count=counts["waves"],
            successful_urls=counts["assets"],
        )
    except (RunStateError, StaleRunRevisionError, SmartResumeError) as exc:
        logger.error("smart-run resume failed: %s", exc)
        return orchestrator._failed_result(run_id, str(exc))
