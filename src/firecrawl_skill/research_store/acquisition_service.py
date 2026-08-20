"""Compatibility facade for the canonical acquisition application service."""

from .acquisition.adapters.bounded_firecrawl import BoundedFirecrawlSearchAdapter
from .acquisition.service import (
    AcquisitionAuthorityChangedError,
    AcquisitionConcurrencyError,
    AcquisitionIdempotencyConflictError,
    AcquisitionResult,
    AcquisitionService,
    SearchProvenanceError,
)

# Historical callers imported this name from acquisition_service. Its effective
# runtime identity has been the bounded adapter since issue #216; preserve that
# contract explicitly rather than mutating this module from package __init__.
FirecrawlSearchAdapter = BoundedFirecrawlSearchAdapter

__all__ = [
    "AcquisitionAuthorityChangedError",
    "AcquisitionConcurrencyError",
    "AcquisitionIdempotencyConflictError",
    "AcquisitionResult",
    "AcquisitionService",
    "FirecrawlSearchAdapter",
    "SearchProvenanceError",
]
