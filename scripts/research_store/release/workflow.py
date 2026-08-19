"""Canonical workflow-benchmark boundary for issue #265.

``research_store.workflow_benchmark`` carries reviewed path-keyed Pyrefly debt.
Keep that implementation path stable rather than laundering the baseline;
this module provides the canonical release namespace until #269 removes the
compatibility scaffolding.
"""

from __future__ import annotations

import sys as _sys

from .. import workflow_benchmark as _impl
from ..workflow_benchmark import (
    BenchmarkDatasetLoader,
    DeterministicIntegrityChecker,
    WorkflowBenchmarkConfig,
    WorkflowBenchmarkResult,
    WorkflowBenchmarkRunner,
    load_benchmark_dataset,
    run_benchmark,
)

__all__ = [
    "BenchmarkDatasetLoader",
    "DeterministicIntegrityChecker",
    "WorkflowBenchmarkConfig",
    "WorkflowBenchmarkResult",
    "WorkflowBenchmarkRunner",
    "load_benchmark_dataset",
    "run_benchmark",
]

_sys.modules[__name__] = _impl
