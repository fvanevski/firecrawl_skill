"""Canonical release-evidence boundary for issue #265.

``research_store.release_evidence`` carries reviewed path-keyed Pyrefly debt.
Keep that implementation path stable rather than laundering the baseline;
this module provides the canonical release namespace until #269 removes the
compatibility scaffolding.
"""

from __future__ import annotations

import sys as _sys

from .. import release_evidence as _impl
from ..release_evidence import (
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
