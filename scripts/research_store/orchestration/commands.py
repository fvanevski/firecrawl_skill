"""Command objects for research orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RunResearchCommand:
    """Immutable command for starting a research run.

    Matches the existing public contract of ``ResearchOrchestrator.run``.
    """

    query: str
    external_run_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict, compare=False)
