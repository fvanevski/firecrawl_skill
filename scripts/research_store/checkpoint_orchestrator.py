"""Orchestrator adapter for recoverable persisted indexing checkpoints."""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from .checkpoint_indexing_stage import (
    INDEX_CHECKPOINT_PENDING_PREFIX,
    CheckpointIndexingStage,
)
from .orchestrator import OrchestratorResult, ResearchOrchestrator
from .stages import StageResult

logger = logging.getLogger(__name__)


class CheckpointResearchOrchestrator(ResearchOrchestrator):
    """Use durable checkpoints when the run service exposes that capability."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if getattr(self.run_service, "checkpoint_indexing_enabled", False) is True:
            self._indexing = CheckpointIndexingStage(
                self.run_service,
                self.config,
                corpus_service=self.corpus_service,
            )
            self._stages["indexing"] = self._indexing

    def _execute_stage(
        self,
        stage_name: str,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        """Execute a stage without fabricating a provider search response."""
        stage = self._stages.get(stage_name)
        if stage is None:
            return StageResult.failed("unknown", f"unknown stage: {stage_name}")

        start = time.monotonic()
        result = stage.execute(
            run_id, run_revision, coverage_revision, run_state, context
        )
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

    def _failed_result(self, run_id: UUID, error: str) -> OrchestratorResult:
        if error.startswith(INDEX_CHECKPOINT_PENDING_PREFIX):
            status = self.run_service.status(run_id=run_id)
            return OrchestratorResult(
                run_id=run_id,
                final_state=status.state,
                outcome="resumable",
                coverage_revision=getattr(status, "current_coverage_revision", None),
                error=None,
            )
        return super()._failed_result(run_id, error)
