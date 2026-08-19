"""Temporary compatibility facade for the assessment coverage service.

Issue #264 moved authoritative coverage ownership to
``research_store.assessment.coverage``.  #269 owns removal of this legacy flat
import path.
"""

from .assessment.coverage import (
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
