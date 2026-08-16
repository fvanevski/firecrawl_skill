"""Checkpoint stage-execution responsibility.

This module contains the canonical implementation of checkpoint-aware
stage execution and failure handling.  ``CheckpointResearchOrchestrator``
delegates to these functions.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from ..checkpoint_indexing_stage import INDEX_CHECKPOINT_PENDING_PREFIX
from ..orchestrator import OrchestratorResult, ResearchOrchestrator
from ..stages import StageResult

logger = logging.getLogger(__name__)


def checkpoint_execute_stage(
    orchestrator: ResearchOrchestrator,
    stage_name: str,
    run_id: UUID,
    run_revision: int,
    coverage_revision: int | None,
    run_state: str,
    context: dict[str, Any],
) -> StageResult:
    """Execute a stage without fabricating a provider search response.

    Unlike the base ``_execute_stage``, this does NOT call
    ``run_service.record_search_response`` (avoids fabricating provider/search
    provenance for internal stage invocations).
    """
    stage = orchestrator._stages.get(stage_name)
    if stage is None:
        return StageResult.failed("unknown", f"unknown stage: {stage_name}")

    start = time.monotonic()
    result = stage.execute(run_id, run_revision, coverage_revision, run_state, context)
    duration_ms = int((time.monotonic() - start) * 1000)

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


def checkpoint_failed_result(
    orchestrator: ResearchOrchestrator,
    run_id: UUID,
    error: str,
) -> OrchestratorResult:
    """Handle stage failures, treating index-checkpoint-pending as resumable.

    If the error starts with ``INDEX_CHECKPOINT_PENDING_PREFIX``, the run
    is not failed — it is resumable.  Otherwise, delegates to the base
    ``_failed_result``.
    """
    if error.startswith(INDEX_CHECKPOINT_PENDING_PREFIX):
        status = orchestrator.run_service.status(run_id=run_id)
        return OrchestratorResult(
            run_id=run_id,
            final_state=status.state,
            outcome="resumable",
            coverage_revision=getattr(status, "current_coverage_revision", None),
            error=None,
        )
    # Delegate to the base class implementation
    from ..orchestrator import ResearchOrchestrator as _Base

    return _Base._failed_result(orchestrator, run_id, error)
