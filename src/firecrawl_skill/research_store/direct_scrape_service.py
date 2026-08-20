"""Compatibility facade for the canonical direct-scrape capability."""

from .acquisition.adapters.firecrawl_scrape import FirecrawlDirectScrapeAdapter
from .acquisition.direct_scrape import (
    DIRECT_SCRAPE_TABLE_PRIVILEGES,
    DirectScrapeError,
    DirectScrapePersistenceError,
    DirectScrapeService,
    _ResolvedTarget,
    build_direct_scrape_service,
    require_direct_scrape_persistence,
)
from .acquisition.models import (
    DirectScrapeBatchResult,
    DirectScrapeItemResult,
    DirectScrapeRequest,
    ScrapeTransportResult,
)

__all__ = [
    "DIRECT_SCRAPE_TABLE_PRIVILEGES",
    "DirectScrapeBatchResult",
    "DirectScrapeError",
    "DirectScrapeItemResult",
    "DirectScrapePersistenceError",
    "DirectScrapeRequest",
    "DirectScrapeService",
    "FirecrawlDirectScrapeAdapter",
    "ScrapeTransportResult",
    "_ResolvedTarget",
    "build_direct_scrape_service",
    "require_direct_scrape_persistence",
]
