"""Temporary compatibility facade for authoritative report artifacts."""

from .reporting.artifacts import (
    ReportArtifactError,
    ReportArtifactService,
)

__all__ = ["ReportArtifactError", "ReportArtifactService"]
