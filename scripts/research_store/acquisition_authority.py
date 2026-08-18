"""Compatibility facade for the canonical acquisition authority module."""

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
