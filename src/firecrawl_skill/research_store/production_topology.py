"""Leaf production-topology primitives shared by canonical and legacy builders.

This module exists to preserve historical direct-builder defaults without
introducing a dependency from application/orchestrator code back to the
canonical ``research_store.composition`` root.  It owns only concrete adapter
injection for the bounded extraction stage: no ``StoreConfig`` resolution,
service/UoW construction, persistence, workflow policy, or transaction logic.
"""

from __future__ import annotations

from typing import Any

from .acquisition.adapters.bounded_firecrawl import BoundedFirecrawlSearchAdapter
from .bounded_orchestrator import BoundedExtractionStage


class ProductionBoundedExtractionStage(BoundedExtractionStage):
    """Production bounded extraction with explicit Firecrawl composition."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("scrape_adapter", BoundedFirecrawlSearchAdapter())
        super().__init__(*args, **kwargs)


__all__ = ["ProductionBoundedExtractionStage"]
