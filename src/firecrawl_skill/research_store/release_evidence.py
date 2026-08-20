"""Temporary compatibility facade for release evidence.

Issue #265 moved the authoritative implementation to
``research_store.release.evidence``. #269 owns removal of this legacy flat
import path after supported callers migrate.
"""

from __future__ import annotations

import sys as _sys

from .release import evidence as _impl
from .release.evidence import (
    MANIFEST_SCHEMA_VERSION,
    REQUIRED_CI_JOBS,
    ArtifactReference,
    CiJobResult,
    Fingerprint,
    ReleaseEvidenceGenerator,
    ReleaseEvidenceManifest,
    ReleaseEvidenceVerifier,
    VerificationResult,
    compute_required_ci_jobs,
)

_manifest_from_dict = _impl._manifest_from_dict
_manifest_to_dict = _impl._manifest_to_dict

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "REQUIRED_CI_JOBS",
    "ArtifactReference",
    "CiJobResult",
    "Fingerprint",
    "ReleaseEvidenceGenerator",
    "ReleaseEvidenceManifest",
    "ReleaseEvidenceVerifier",
    "VerificationResult",
    "compute_required_ci_jobs",
]

if __name__ == "__main__":
    ReleaseEvidenceGenerator.main()
else:
    _sys.modules[__name__] = _impl
