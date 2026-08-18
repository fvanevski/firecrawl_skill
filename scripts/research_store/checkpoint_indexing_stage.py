"""Compatibility alias for canonical projection checkpoint orchestration."""

import sys as _sys

from .retrieval.projection import checkpoint_indexing_stage as _canonical
from .retrieval.projection.checkpoint_indexing_stage import (
    INDEX_CHECKPOINT_PENDING_PREFIX,
    CheckpointIndexingStage,
    IndexCheckpointPending,
    _PersistingCheckpointRunner,
    _raise_pending,
)

__all__ = [
    "INDEX_CHECKPOINT_PENDING_PREFIX",
    "CheckpointIndexingStage",
    "IndexCheckpointPending",
]

_sys.modules[__name__] = _canonical
