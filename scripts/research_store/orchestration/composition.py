"""Production topology composition.

This module builds the production orchestrator explicitly, without relying
on import-time rebinding.  It constructs a ``CheckpointResearchOrchestrator``
with bounded stage classes injected.
"""

from __future__ import annotations

from ..bounded_orchestrator import BoundedAcquisitionStage, BoundedExtractionStage
from ..checkpoint_orchestrator import CheckpointResearchOrchestrator
from ..config import StoreConfig
from ..orchestrator import OrchestratorConfig, ResearchOrchestrator


def build_production_orchestrator(
    config: StoreConfig,
    *,
    orchestrator_config: OrchestratorConfig | None = None,
) -> ResearchOrchestrator:
    """Build the production orchestrator with bounded stages.

    This is the explicit composition root that replaces the old import-time
    rebinding pattern.  It constructs a ``CheckpointResearchOrchestrator``
    with ``BoundedAcquisitionStage`` and ``BoundedExtractionStage`` injected.

    Args:
        config: Store configuration.
        orchestrator_config: Orchestrator-specific settings.

    Returns:
        A fully wired production orchestrator.
    """
    return CheckpointResearchOrchestrator.build(
        config,
        orchestrator_config=orchestrator_config,
        acquisition_stage_cls=BoundedAcquisitionStage,
        extraction_stage_cls=BoundedExtractionStage,
    )
