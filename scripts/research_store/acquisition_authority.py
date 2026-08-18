"""Compatibility facade for the canonical acquisition authority module."""

# Historical authority tests/callers patch attributes on these module
# objects. Expose the shared stdlib module singletons without restoring
# duplicate authority implementation.
import os  # noqa: F401
import tempfile  # noqa: F401

from .acquisition.authority import (
    ACQUISITION_ENTRY_STATES,
    ACQUISITION_TABLE_PRIVILEGES,
    AcquisitionPreflightError,
    AuthoritativeAcquisitionContext,
    require_authoritative_acquisition,
)

__all__ = [
    "ACQUISITION_ENTRY_STATES",
    "ACQUISITION_TABLE_PRIVILEGES",
    "AcquisitionPreflightError",
    "AuthoritativeAcquisitionContext",
    "require_authoritative_acquisition",
]
