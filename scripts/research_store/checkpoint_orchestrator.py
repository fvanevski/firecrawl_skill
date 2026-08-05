"""Orchestrator adapter for recoverable persisted indexing checkpoints."""

from __future__ import annotations

from uuid import UUID

from .checkpoint_indexing_stage import INDEX_CHECKPOINT_PENDING_PREFIX
from .orchestrator import OrchestratorResult, ResearchOrchestrator


class CheckpointResearchOrchestrator(ResearchOrchestrator):
    """Return a resumable result without terminalizing bounded index work."""

    def _failed_result(self, run_id: UUID, error: str) -> OrchestratorResult:
        if error.startswith(INDEX_CHECKPOINT_PENDING_PREFIX):
            status = self.run_service.status(run_id=run_id)
            return OrchestratorResult(
                run_id=run_id,
                final_state=status.state,
                outcome="resumable",
                coverage_revision=getattr(
                    status, "current_coverage_revision", None
                ),
                error=None,
            )
        return super()._failed_result(run_id, error)
