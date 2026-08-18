"""Production topology composition.

This module builds production orchestrators explicitly, without relying on
import-time rebinding. Fresh and resumable paths both preserve checkpoint
indexing behavior and bounded acquisition/extraction.
"""

from __future__ import annotations

from typing import Any

from ..acquisition.adapters.bounded_firecrawl import BoundedFirecrawlSearchAdapter
from ..bounded_orchestrator import BoundedAcquisitionStage, BoundedExtractionStage
from ..checkpoint_orchestrator import CheckpointResearchOrchestrator
from ..config import StoreConfig
from ..orchestrator import OrchestratorConfig, ResearchOrchestrator


class ProductionBoundedExtractionStage(BoundedExtractionStage):
    """Production bounded extraction with explicit Firecrawl composition."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("scrape_adapter", BoundedFirecrawlSearchAdapter())
        super().__init__(*args, **kwargs)


def build_production_orchestrator(
    config: StoreConfig,
    *,
    orchestrator_config: OrchestratorConfig | None = None,
) -> ResearchOrchestrator:
    """Build the fresh production orchestrator with bounded stages."""
    return CheckpointResearchOrchestrator.build(
        config,
        orchestrator_config=orchestrator_config,
        acquisition_stage_cls=BoundedAcquisitionStage,
        extraction_stage_cls=ProductionBoundedExtractionStage,
    )


def build_production_resumable_orchestrator(
    config: StoreConfig,
    *,
    orchestrator_config: OrchestratorConfig | None = None,
):
    """Build the production smart-resume orchestrator explicitly.

    Importing the provenance facade lazily avoids a package cycle while making
    the production caller's topology explicit. The provenance class itself also
    defaults its historical direct ``build`` surface to bounded stages, so
    callers cannot silently fall back to the unbounded base pair.
    """
    from ..search_provenance import ProvenanceResumableResearchOrchestrator

    return ProvenanceResumableResearchOrchestrator.build(
        config,
        orchestrator_config=orchestrator_config,
        acquisition_stage_cls=BoundedAcquisitionStage,
        extraction_stage_cls=ProductionBoundedExtractionStage,
    )


__all__ = [
    "ProductionBoundedExtractionStage",
    "build_production_orchestrator",
    "build_production_resumable_orchestrator",
]
