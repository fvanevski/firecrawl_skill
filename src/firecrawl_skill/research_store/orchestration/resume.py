"""Canonical resume orchestration lifecycle.

This module contains the single authoritative implementation of the
resume control flow. ``ResumableResearchOrchestrator.run`` delegates to
``run_resume`` to avoid duplicating the logic.

State queries are routed through ``ResumeStatePort`` so that no raw SQL
lives in this package. Deterministic reconstruction helpers live in
``resume_support`` and have no dependency on the compatibility facade.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ..candidate_budget_outcomes import (
    CandidateBudgetHardRejected,
    CandidateBudgetOverrideRequired,
)
from ..orchestrator import OrchestratorResult, ResearchOrchestrator
from ..run_service import RunStateError, StaleRunRevisionError
from ..smart_result import OperatorActionOrchestratorResult
from ..stages import ContextKeys
from ..temporal_coverage import (
    diagnose_temporal_coverage,
    should_classify_temporal_gap,
    temporal_gap_payload,
)
from .commands import RunResearchCommand
from .ports import ResumeStatePort
from .resume_support import (
    PLANNING_STATES,
    TERMINAL_STATES,
    SmartResumeError,
    coverage_context,
    replay_extraction_inputs,
)

if TYPE_CHECKING:
    from ..smart_orchestrator import ResumableResearchOrchestrator

logger = logging.getLogger(__name__)

_TEMPORAL_GAP_EVENT = "evidence.temporal_coverage_gap"
_TEMPORAL_RESOLVED_EVENT = "evidence.temporal_coverage_resolved"


def _operator_action_result(
    state_port: ResumeStatePort,
    run_id,
    state: str,
    coverage_revision: int | None,
    exc: CandidateBudgetOverrideRequired,
) -> OperatorActionOrchestratorResult:
    counts = state_port.counts(run_id)
    return OperatorActionOrchestratorResult(
        run_id=run_id,
        final_state=state,
        outcome="operator_action_required",
        coverage_revision=coverage_revision,
        wave_count=counts.waves,
        successful_urls=counts.assets,
        operator_action=exc.to_dict(),
    )


def _temporal_operator_action_result(
    state_port: ResumeStatePort,
    run_id,
    state: str,
    coverage_revision: int | None,
    gap: dict[str, Any],
) -> OperatorActionOrchestratorResult:
    counts = state_port.counts(run_id)
    return OperatorActionOrchestratorResult(
        run_id=run_id,
        final_state=state,
        outcome="operator_action_required",
        coverage_revision=coverage_revision,
        wave_count=counts.waves,
        successful_urls=counts.assets,
        operator_action=dict(gap),
    )


def _active_temporal_gap(state_port: ResumeStatePort, run_id) -> dict[str, Any] | None:
    """Read the optional new resume-port method without breaking older test doubles."""

    reader = getattr(state_port, "temporal_coverage_gap", None)
    if not callable(reader):
        return None
    gap = reader(run_id)
    if gap is None:
        return None
    if not isinstance(gap, dict) or gap.get("kind") != "temporal_coverage_gap":
        raise SmartResumeError("resume state returned malformed temporal coverage gap")
    return dict(gap)


def _normalized_chunk_ids(assets: list[dict[str, Any]]) -> list[UUID]:
    """Restore persisted chunk identifiers to the canonical ``UUID`` form.

    Resume reconstruction restores chunk membership in serialized string form
    (``resume_state_repository``), while the canonical passage-selection API
    is typed around ``list[UUID]``. A malformed identifier is skipped with a
    visible log instead of breaking the purely explanatory gap path.
    """
    chunk_ids: list[UUID] = []
    for asset in assets:
        chunks = list(asset.get("chunk_ids", ()))
        if not chunks:
            continue
        raw = chunks[0]
        try:
            chunk_ids.append(UUID(str(raw)))
        except (TypeError, ValueError):
            logger.warning(
                "resume chunk identifier %r is not a canonical UUID; "
                "skipping it for temporal gap classification",
                raw,
            )
            continue
    return chunk_ids


def _temporal_gap_from_authority(
    orchestrator: ResearchOrchestrator,
    state_port: ResumeStatePort,
    run_id,
    spec: dict[str, Any],
    coverage_revision: int | None,
) -> dict[str, Any] | None:
    """Reproduce the bounded evidence passage selection and classify it purely."""

    corpus = getattr(orchestrator, "corpus_service", None)
    if corpus is None:
        return None
    chunk_ids = _normalized_chunk_ids(state_port.assets(run_id))
    if not chunk_ids:
        return None
    _execution, passages = corpus.select_run_passages(
        run_id,
        chunk_ids,
        max_tokens=3000,
        max_passages=min(20, len(chunk_ids)),
    )
    if not should_classify_temporal_gap(passages, spec):
        return None
    diagnostics = diagnose_temporal_coverage(passages, spec)
    if diagnostics.qualifying_passages:
        return None
    return temporal_gap_payload(
        diagnostics,
        coverage_revision=coverage_revision,
    )


def _persist_temporal_gap(
    orchestrator: ResearchOrchestrator,
    run_id,
    run_revision: int,
    gap: dict[str, Any],
) -> None:
    canonical = json.dumps(gap, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    with orchestrator.run_service.uow_factory() as uow:
        uow.runs.append_event(
            run_id,
            _TEMPORAL_GAP_EVENT,
            "orchestrator",
            f"temporal-gap:{run_id}:r{run_revision}:{digest}",
            actor_identifier="ResumableResearchOrchestrator",
            payload={"temporal_coverage_gap": gap},
        )
        uow.commit()


def _persist_temporal_resolution(
    orchestrator: ResearchOrchestrator,
    run_id,
    run_revision: int,
    coverage_revision: int | None,
) -> None:
    with orchestrator.run_service.uow_factory() as uow:
        uow.runs.append_event(
            run_id,
            _TEMPORAL_RESOLVED_EVENT,
            "orchestrator",
            f"temporal-gap-resolved:{run_id}:r{run_revision}:c{coverage_revision or 0}",
            actor_identifier="ResumableResearchOrchestrator",
            payload={
                "kind": "temporal_coverage_resolved",
                "coverage_revision": coverage_revision,
            },
        )
        uow.commit()


def run_resume(
    orchestrator: ResumableResearchOrchestrator,
    command: RunResearchCommand,
    *,
    state_port: ResumeStatePort,
) -> OrchestratorResult:
    """Execute the canonical persisted smart-run continuation lifecycle."""
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
    persisted_gap = _active_temporal_gap(state_port, run_id)
    if persisted_gap is not None:
        ctx["temporal_coverage_gap"] = persisted_gap

    if state in TERMINAL_STATES:
        return OrchestratorResult(
            run_id=run_id,
            final_state=state,
            outcome=state,
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
                prior_gap = _active_temporal_gap(state_port, run_id)
                result = orchestrator._execute_stage(
                    "evidence_preparation",
                    run_id,
                    revision,
                    coverage_revision,
                    state,
                    ctx,
                )
                if result.error:
                    gap = _temporal_gap_from_authority(
                        orchestrator,
                        state_port,
                        run_id,
                        spec,
                        coverage_revision,
                    )
                    if gap is None:
                        return orchestrator._failed_result(run_id, result.error)
                    _persist_temporal_gap(orchestrator, run_id, revision, gap)
                    ctx["temporal_coverage_gap"] = gap
                    state, revision = orchestrator._refresh(run_id)
                    if int(ctx.get(ContextKeys.WAVE_COUNT, 0)) >= max_cycles:
                        return _temporal_operator_action_result(
                            state_port,
                            run_id,
                            state,
                            coverage_revision,
                            gap,
                        )
                    continue
                if prior_gap is not None:
                    _persist_temporal_resolution(
                        orchestrator,
                        run_id,
                        revision,
                        coverage_revision,
                    )
                    ctx.pop("temporal_coverage_gap", None)
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
                active_gap = _active_temporal_gap(state_port, run_id)
                if active_gap is not None:
                    ctx["temporal_coverage_gap"] = active_gap
                else:
                    ctx.pop("temporal_coverage_gap", None)
                if int(ctx.get(ContextKeys.WAVE_COUNT, 0)) >= max_cycles:
                    ctx["_budget_exhausted"] = True
                    if active_gap is not None:
                        return _temporal_operator_action_result(
                            state_port,
                            run_id,
                            state,
                            coverage_revision,
                            active_gap,
                        )
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
    except CandidateBudgetOverrideRequired as exc:
        logger.info("smart-run requires exact candidate-budget override: %s", exc)
        return _operator_action_result(
            state_port, run_id, state, coverage_revision, exc
        )
    except CandidateBudgetHardRejected as exc:
        logger.error("smart-run candidate budget rejected: %s", exc)
        return orchestrator._failed_result(run_id, str(exc))
    except (RunStateError, StaleRunRevisionError, SmartResumeError) as exc:
        logger.error("smart-run resume failed: %s", exc)
        return orchestrator._failed_result(run_id, str(exc))
