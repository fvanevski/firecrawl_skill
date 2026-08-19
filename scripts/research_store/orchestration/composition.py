"""Temporary compatibility facade for the canonical production composition root."""

from ..composition import (
    ProductionBoundedExtractionStage,
    build_production_orchestrator,
    build_production_resumable_orchestrator,
)

__all__ = [
    "ProductionBoundedExtractionStage",
    "build_production_orchestrator",
    "build_production_resumable_orchestrator",
]
