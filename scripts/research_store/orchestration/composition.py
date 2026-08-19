"""Temporary compatibility facade for canonical production composition."""

from ..composition import (
    build_production_orchestrator,
    build_production_resumable_orchestrator,
)
from ..production_topology import ProductionBoundedExtractionStage

__all__ = [
    "ProductionBoundedExtractionStage",
    "build_production_orchestrator",
    "build_production_resumable_orchestrator",
]
