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
    """Use durable checkpoints and bounded production stages by default.

    Before issue #261, package initialization rebound the base orchestrator stage
    globals before this public compatibility class was constructed.  The explicit
    builder below preserves that production default without mutating module state.
    Callers may still inject alternate stage classes deliberately for tests or
    non-production composition.
    """

    @classmethod
    def build(
        cls,
        config=None,
        *,
        orchestrator_config=None,
        corpus_service=None,
        terminal_config=None,
        acquisition_stage_cls=None,
        extraction_stage_cls=None,
        indexing_stage_cls=None,
    ):
        """Build checkpoint orchestration with bounded acquisition/extraction."""
        if acquisition_stage_cls is None:
            from .bounded_orchestrator import BoundedAcquisitionStage

            acquisition_stage_cls = BoundedAcquisitionStage
        if extraction_stage_cls is None:
            from .orchestration.composition import ProductionBoundedExtractionStage

            extraction_stage_cls = ProductionBoundedExtractionStage
        return super().build(
            config,
            orchestrator_config=orchestrator_config,
            corpus_service=corpus_service,
            terminal_config=terminal_config,
            acquisition_stage_cls=acquisition_stage_cls,
            extraction_stage_cls=extraction_stage_cls,
            indexing_stage_cls=indexing_stage_cls,
        )

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
