"""Acquisition capability boundary.

Application policy, authority snapshots, transport-facing models, and ports live
under this package. Concrete Firecrawl/network implementations live below
:mod:`research_store.acquisition.adapters` and are selected only by composition
roots or explicit compatibility facades.
"""

from .authority import (
    ACQUISITION_ENTRY_STATES,
    ACQUISITION_TABLE_PRIVILEGES,
    AcquisitionPreflightError,
    AuthoritativeAcquisitionContext,
    require_authoritative_acquisition,
)
from .models import (
    AcquisitionResult,
    DirectScrapeBatchResult,
    DirectScrapeItemResult,
    DirectScrapeRequest,
    ScrapeTransportResult,
    SearchAdapterResult,
)
from .ports import DirectScrapeAdapter, SearchAdapter

__all__ = [
    "ACQUISITION_ENTRY_STATES",
    "ACQUISITION_TABLE_PRIVILEGES",
    "AcquisitionPreflightError",
    "AcquisitionResult",
    "AuthoritativeAcquisitionContext",
    "DirectScrapeAdapter",
    "DirectScrapeBatchResult",
    "DirectScrapeItemResult",
    "DirectScrapeRequest",
    "ScrapeTransportResult",
    "SearchAdapter",
    "SearchAdapterResult",
    "require_authoritative_acquisition",
]
