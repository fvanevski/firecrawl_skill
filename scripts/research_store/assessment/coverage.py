"""Coverage-led assessment boundary.

The implementation remains at its established flat path during the structural
campaign so callers and the path-keyed validation surface stay stable.  This
module is the canonical capability entry point for new code; #269 owns final
compatibility-facade cleanup.
"""

from ..coverage_service import (
    CoverageError,
    CoverageEvent,
    CoverageService,
    CoverageSnapshot,
    DuplicateCoverageEventError,
    StaleCoverageRevisionError,
    UnknownCoverageItemError,
)

__all__ = [
    "CoverageError",
    "CoverageEvent",
    "CoverageService",
    "CoverageSnapshot",
    "DuplicateCoverageEventError",
    "StaleCoverageRevisionError",
    "UnknownCoverageItemError",
]
