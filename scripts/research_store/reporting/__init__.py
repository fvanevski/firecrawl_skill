"""Canonical report construction, validation, and persistence slice.

The large synthesis and validation implementations remain at their reviewed,
baseline-tracked paths in issue #264.  The package establishes the canonical
reporting capability boundary without changing report/evidence revision,
citation, artifact, or completion semantics.  #269 owns final facade cleanup.
"""

from .artifacts import ReportArtifactService
from .construction import LocalSynthesisService
from .validation import (
    ClaimResolution,
    ReportValidationFinding,
    ReportValidationResult,
    ReportValidationSeverity,
    ReportValidator,
)

__all__ = [
    "ClaimResolution",
    "LocalSynthesisService",
    "ReportArtifactService",
    "ReportValidationFinding",
    "ReportValidationResult",
    "ReportValidationSeverity",
    "ReportValidator",
]
