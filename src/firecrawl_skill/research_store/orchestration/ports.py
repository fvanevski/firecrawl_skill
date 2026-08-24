"""Narrow ports for the resume orchestration lifecycle.

This module defines the minimal read and orchestration capabilities used by the
canonical resume lifecycle. It intentionally does NOT duplicate the broad
repository protocols in ``research_store.ports`` or depend on the smart-search
composition facade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class ResumeCounts:
    """Aggregate counts for a run's persisted resume state."""

    waves: int
    attempts: int
    assets: int


class ResumeStatePort(Protocol):
    """Read-only port for querying run state during resume."""

    def counts(self, run_id: UUID) -> ResumeCounts:
        """Return wave/attempt/asset counts for the run."""
        ...

    def authorized_queries(self, run_id: UUID) -> list[dict[str, Any]]:
        """Return the list of authorized search queries for this run."""
        ...

    def completed_candidates(self, run_id: UUID) -> set[str]:
        """Return candidate IDs that already have completed extraction."""
        ...

    def assets(self, run_id: UUID) -> list[dict[str, Any]]:
        """Return persisted asset references for this run."""
        ...

    def packet_revision(self, run_id: UUID) -> int:
        """Return the latest evidence packet revision number."""
        ...

    def temporal_coverage_gap(self, run_id: UUID) -> dict[str, Any] | None:
        """Return the active persisted temporal gap, if it has not been resolved."""
        ...


class ResumeOrchestratorPort(Protocol):
    """Minimal application-facing orchestration surface required by resume."""

    orchestrator_config: Any
    run_service: Any
    coverage_service: Any
    corpus_service: Any

    def _refresh(self, run_id: UUID) -> tuple[str, int]:
        """Return the authoritative run state and lifecycle revision."""
        ...

    def _execute_stage(
        self,
        stage_name: str,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> Any:
        """Execute one canonical stage under the current authoritative revision."""
        ...

    def _checkpoint(
        self,
        run_id: UUID,
        context: dict[str, Any],
        state: str,
    ) -> Any:
        """Return a checkpoint disposition when the configured boundary requires it."""
        ...

    def _failed_result(self, run_id: UUID, error: str) -> Any:
        """Return the canonical failed orchestration result."""
        ...
