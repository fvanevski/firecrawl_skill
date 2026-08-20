"""Research orchestration package.

This package contains the canonical orchestration lifecycles, command
objects, ports, and production composition.  It is the single source of
truth for the orchestration control flow.
"""

from firecrawl_skill.research_store.composition import build_production_orchestrator

from ..orchestration.checkpoint import (
    checkpoint_execute_stage,
    checkpoint_failed_result,
)
from ..orchestration.commands import RunResearchCommand
from ..orchestration.lifecycle import run_research
from ..orchestration.ports import (
    ResumeCounts,
    ResumeStatePort,
)
from ..orchestration.resume import run_resume

__all__ = [
    "ResumeCounts",
    "ResumeStatePort",
    "RunResearchCommand",
    "build_production_orchestrator",
    "checkpoint_execute_stage",
    "checkpoint_failed_result",
    "run_research",
    "run_resume",
]
