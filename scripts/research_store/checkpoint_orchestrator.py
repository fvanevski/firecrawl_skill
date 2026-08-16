"""Orchestrator adapter for recoverable persisted indexing checkpoints."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from .checkpoint_indexing_stage import CheckpointIndexingStage
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
        """Execute a stage without fabricating a provider search response.

        Thin facade delegating to ``orchestration.checkpoint.checkpoint_execute_stage``.
        """
        from .orchestration.checkpoint import checkpoint_execute_stage

        return checkpoint_execute_stage(
            self,
            stage_name,
            run_id,
            run_revision,
            coverage_revision,
            run_state,
            context,
        )

    def _failed_result(self, run_id: UUID, error: str) -> OrchestratorResult:
        """Handle failures, treating index-checkpoint-pending as resumable.

        Thin facade delegating to ``orchestration.checkpoint.checkpoint_failed_result``.
        """
        from .orchestration.checkpoint import checkpoint_failed_result

        return checkpoint_failed_result(self, run_id, error)
