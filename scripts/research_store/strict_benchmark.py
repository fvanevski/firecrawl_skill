"""Temporary compatibility facade for the strict release campaign.

Issue #265 moved the authoritative implementation to
``research_store.release.strict``. #269 owns removal of this legacy flat import
path after supported callers migrate.
"""

from __future__ import annotations

import sys as _sys

from .release import strict as _impl
from .release.strict import (
    DEFAULT_DATASET,
    RELEASE_MODES,
    REPO_ROOT,
    SCRIPTS,
    MetricStatus,
    ReleaseBenchmarkConfig,
    ReleaseBenchmarkResult,
    ReleaseBenchmarkRunner,
    ReproducibilityComparison,
    _build_env_manifest,
    _build_manifest,
    _compare_campaigns,
    _compute_file_hash,
    _get_full_sha,
    _get_tree_hash,
    _preflight_check,
    _qdrant_compatibility_errors,
    _run_campaign,
    _write_json_atomic,
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())

_sys.modules[__name__] = _impl
