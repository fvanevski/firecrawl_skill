"""Deterministic report/citation validation boundary.

``report_validator.py`` has reviewed path-keyed Pyrefly debt.  The canonical
reporting namespace delegates to that exact implementation until the debt is
fixed and #269 removes compatibility facades.
"""

from ..report_validator import (
    ClaimResolution,
    ReportValidationFinding,
    ReportValidationResult,
    ReportValidationSeverity,
    ReportValidator,
)

__all__ = [
    "ClaimResolution",
    "ReportValidationFinding",
    "ReportValidationResult",
    "ReportValidationSeverity",
    "ReportValidator",
]
