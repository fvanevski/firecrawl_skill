"""Temporary compatibility facade for strict release campaigns.

Issue #265 moved authoritative strict-campaign ownership to
``research_store.release.strict``.  #269 owns removal of this legacy flat
import path.
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
    _get_firecrawl_version,
    _get_full_sha,
    _get_tree_hash,
    _legacy_preflight_check,
    _preflight_check,
    _qdrant_compatibility_errors,
    _run_campaign,
    _write_json_atomic,
    load_benchmark_dataset,
    main,
)

__all__ = [
    "DEFAULT_DATASET",
    "RELEASE_MODES",
    "REPO_ROOT",
    "SCRIPTS",
    "MetricStatus",
    "ReleaseBenchmarkConfig",
    "ReleaseBenchmarkResult",
    "ReleaseBenchmarkRunner",
    "ReproducibilityComparison",
    "_build_env_manifest",
    "_build_manifest",
    "_compare_campaigns",
    "_compute_file_hash",
    "_get_firecrawl_version",
    "_get_full_sha",
    "_get_tree_hash",
    "_legacy_preflight_check",
    "_preflight_check",
    "_qdrant_compatibility_errors",
    "_run_campaign",
    "_write_json_atomic",
    "load_benchmark_dataset",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
else:
    _sys.modules[__name__] = _impl
