"""Temporary compatibility facade for workflow benchmark infrastructure.

Issue #265 moved authoritative workflow-benchmark ownership to
``research_store.release.workflow``.  #269 owns removal of this legacy flat
import path.
"""

from __future__ import annotations

import sys as _sys

from .release import workflow as _impl
from .release.workflow import (
    BenchmarkDatasetLoader,
    DeterministicIntegrityChecker,
    WorkflowBenchmarkConfig,
    WorkflowBenchmarkResult,
    WorkflowBenchmarkRunner,
    _build_dataset,
    _build_objective,
    load_benchmark_dataset,
    run_benchmark,
)

__all__ = [
    "BenchmarkDatasetLoader",
    "DeterministicIntegrityChecker",
    "WorkflowBenchmarkConfig",
    "WorkflowBenchmarkResult",
    "WorkflowBenchmarkRunner",
    "_build_dataset",
    "_build_objective",
    "load_benchmark_dataset",
    "run_benchmark",
]

if __name__ != "__main__":
    _sys.modules[__name__] = _impl
