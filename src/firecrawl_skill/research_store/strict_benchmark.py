"""Temporary compatibility facade for the strict release campaign.

Issue #265 moved the authoritative implementation to
``research_store.release.strict``. #269 owns removal of this legacy flat import
path after supported callers migrate.
"""

from __future__ import annotations

import sys as _sys

from .release import strict as _impl

DEFAULT_DATASET = _impl.DEFAULT_DATASET
RELEASE_MODES = _impl.RELEASE_MODES
REPO_ROOT = _impl.REPO_ROOT
SCRIPTS = _impl.SCRIPTS
MetricStatus = _impl.MetricStatus
ReleaseBenchmarkConfig = _impl.ReleaseBenchmarkConfig
ReleaseBenchmarkResult = _impl.ReleaseBenchmarkResult
ReleaseBenchmarkRunner = _impl.ReleaseBenchmarkRunner
ReproducibilityComparison = _impl.ReproducibilityComparison
_build_env_manifest = _impl._build_env_manifest
_build_manifest = _impl._build_manifest
_compare_campaigns = _impl._compare_campaigns
_compute_file_hash = _impl._compute_file_hash
_get_full_sha = _impl._get_full_sha
_get_tree_hash = _impl._get_tree_hash
_preflight_check = _impl._preflight_check
_qdrant_compatibility_errors = _impl._qdrant_compatibility_errors
_run_campaign = _impl._run_campaign
_write_json_atomic = _impl._write_json_atomic
main = _impl.main

if __name__ == "__main__":
    raise SystemExit(main())

_sys.modules[__name__] = _impl
