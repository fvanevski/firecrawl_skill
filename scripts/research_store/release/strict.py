"""Canonical strict-campaign boundary for issue #265.

``research_store.strict_benchmark`` carries reviewed path-keyed Pyrefly debt.
Keep that implementation path stable rather than laundering the baseline;
this module provides the canonical release namespace until #269 removes the
compatibility scaffolding.
"""

from __future__ import annotations

import sys as _sys

from .. import strict_benchmark as _impl
from ..strict_benchmark import (
    DEFAULT_DATASET,
    RELEASE_MODES,
    REPO_ROOT,
    SCRIPTS,
    MetricStatus,
    ReleaseBenchmarkConfig,
    ReleaseBenchmarkResult,
    ReleaseBenchmarkRunner,
    ReproducibilityComparison,
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
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())

_sys.modules[__name__] = _impl
