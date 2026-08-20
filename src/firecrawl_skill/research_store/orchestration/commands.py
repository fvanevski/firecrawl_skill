"""Command objects for research orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class RunResearchCommand:
    """Immutable command for executing a research run.

    Matches the existing public contract of ``ResearchOrchestrator.run``.
    """

    run_id: UUID
    spec: dict[str, Any]
    search_plan: dict[str, Any]
    max_adaptive_cycles: int | None = None
    context: dict[str, Any] = field(default_factory=dict, compare=False)
