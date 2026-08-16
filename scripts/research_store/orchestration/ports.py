"""Narrow ports for the resume orchestration lifecycle.

This module defines a minimal read-only port that the resume lifecycle
(``run_resume``) uses to query run state.  It intentionally does NOT
duplicate the broad repository protocols in ``research_store.ports``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class RunCounts:
    """Aggregate counts for a run's persisted state."""

    sources: int
    documents: int
    chunks: int
    invocations: int
    transitions: int


@dataclass(frozen=True)
class ExtractionInput:
    """A single extraction input row replayed from the database."""

    source_id: str
    url: str
    content: str
    title: str | None = None
    metadata: dict[str, Any] | None = None


class ResumeStatePort(Protocol):
    """Read-only port for querying run state during resume.

    Implementations must provide these methods.  The resume lifecycle
    uses them to reconstruct execution context without direct SQL.
    """

    def counts(self, run_id: str) -> RunCounts:
        """Return aggregate document/source/chunk/invocation counts."""
        ...

    def authorized_queries(self, run_id: str) -> list[dict[str, Any]]:
        """Return the list of authorized search queries for this run."""
        ...

    def completed_candidates(self, run_id: str) -> list[dict[str, Any]]:
        """Return source IDs that already have completed acquisition."""
        ...

    def extraction_inputs(
        self, run_id: str, context: dict[str, Any]
    ) -> list[ExtractionInput]:
        """Return extraction inputs to replay for this run."""
        ...

    def assets(self, run_id: str) -> list[dict[str, Any]]:
        """Return persisted asset references for this run."""
        ...

    def packet_revision(self, run_id: str) -> int:
        """Return the current strategy packet revision number."""
        ...
