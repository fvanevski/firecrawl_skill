"""Temporary compatibility facade for release-evidence infrastructure.

Issue #265 moved authoritative release-evidence ownership to
``research_store.release.evidence``.  #269 owns removal of this legacy flat
import path.
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
    _commit_count_since,
    _commits_between,
    _current_sha,
    _current_tree_hash,
    _dir_file_count,
    _extract_matrix_vars,
    _file_sha256,
    _git,
    _git_safe,
    _manifest_from_dict,
    _manifest_to_dict,
    _matrix_combinations,
    _sha_at_ref,
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
    "_commit_count_since",
    "_commits_between",
    "_current_sha",
    "_current_tree_hash",
    "_dir_file_count",
    "_extract_matrix_vars",
    "_file_sha256",
    "_git",
    "_git_safe",
    "_manifest_from_dict",
    "_manifest_to_dict",
    "_matrix_combinations",
    "_sha_at_ref",
    "compute_required_ci_jobs",
]

if __name__ == "__main__":
    ReleaseEvidenceGenerator.main()
else:
    _sys.modules[__name__] = _impl
