"""Compatibility import for canonical retrieval projection checkpoint stage."""
from .retrieval.projection.checkpoint_indexing_stage import (
    INDEX_CHECKPOINT_PENDING_PREFIX,
    CheckpointIndexingStage,
    IndexCheckpointPending,
)

__all__ = [
    "INDEX_CHECKPOINT_PENDING_PREFIX",
    "CheckpointIndexingStage",
    "IndexCheckpointPending",
]
