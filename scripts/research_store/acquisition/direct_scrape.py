"""Compatibility facade for authoritative direct-scrape application contracts.

The PostgreSQL-authoritative implementation lives in
``acquisition.direct_scrape_application``.  This historical module path remains
available during Phase 5, but construction delegates lazily to the canonical
``research_store.composition`` root so application code never depends back on
composition.
"""

from __future__ import annotations

from collections.abc import Callable

from ..config import StoreConfig
from .direct_scrape_application import (
    DIRECT_SCRAPE_TABLE_PRIVILEGES,
    DirectScrapeError,
    DirectScrapePersistenceError,
    DirectScrapeService,
    _ResolvedTarget,
    require_direct_scrape_persistence,
)
from .models import (
    DirectScrapeBatchResult,
    DirectScrapeItemResult,
    DirectScrapeRequest,
    ScrapeTransportResult,
)
from .ports import DirectScrapeAdapter


def build_direct_scrape_service(
    config: StoreConfig | None = None,
    *,
    adapter_factory: Callable[[], DirectScrapeAdapter] | None = None,
) -> DirectScrapeService:
    """Delegate the historical builder surface to the canonical root."""
    from .. import composition as _composition

    return _composition.build_direct_scrape_service(
        config,
        adapter_factory=adapter_factory,
    )


__all__ = [
    "DIRECT_SCRAPE_TABLE_PRIVILEGES",
    "DirectScrapeBatchResult",
    "DirectScrapeError",
    "DirectScrapeItemResult",
    "DirectScrapePersistenceError",
    "DirectScrapeRequest",
    "DirectScrapeService",
    "ScrapeTransportResult",
    "_ResolvedTarget",
    "build_direct_scrape_service",
    "require_direct_scrape_persistence",
]
