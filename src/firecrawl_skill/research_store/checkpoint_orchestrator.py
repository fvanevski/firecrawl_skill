"""Orchestrator adapter for recoverable persisted indexing checkpoints.

Production construction is owned by ``research_store.composition``.  This module
contains only the checkpoint-aware application subclass over injected
collaborators.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from firecrawl_skill.research_store.retrieval.projection.checkpoint_indexing_stage import (
    CheckpointIndexingStage,
)

from .orchestrator import OrchestratorResult, ResearchOrchestrator
from .stages import StageResult

logger = logging.getLogger(__name__)


class CheckpointResearchOrchestrator(ResearchOrchestrator):
    """Research orchestrator with durable indexing-checkpoint behavior."""

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
        """Treat an index-checkpoint-pending failure as resumable."""
        from .orchestration.checkpoint import checkpoint_failed_result

        return checkpoint_failed_result(self, run_id, error)
