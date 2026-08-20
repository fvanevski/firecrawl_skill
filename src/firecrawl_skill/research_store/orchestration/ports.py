"""Narrow ports for the resume orchestration lifecycle.

This module defines a minimal read-only port that the resume lifecycle
(``run_resume``) uses to query run state.  It intentionally does NOT
duplicate the broad repository protocols in ``research_store.ports``.
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
    """Read-only port for querying run state during resume.

    Implementations must provide these methods.  The resume lifecycle
    uses them to reconstruct execution context without direct SQL.
    """

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
