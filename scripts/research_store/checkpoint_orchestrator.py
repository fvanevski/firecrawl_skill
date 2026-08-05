"""Orchestrator adapter for recoverable persisted indexing checkpoints."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .checkpoint_indexing_stage import (
    INDEX_CHECKPOINT_PENDING_PREFIX,
    CheckpointIndexingStage,
)
from .orchestrator import OrchestratorResult, ResearchOrchestrator


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
