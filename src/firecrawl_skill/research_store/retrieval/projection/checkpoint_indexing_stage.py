"""Projection namespace facade for baseline-stable checkpoint orchestration."""

from ...checkpoint_indexing_stage import (
    INDEX_CHECKPOINT_PENDING_PREFIX,
    CheckpointIndexingStage,
    IndexCheckpointPending,
)

__all__ = [
    "INDEX_CHECKPOINT_PENDING_PREFIX",
    "CheckpointIndexingStage",
    "IndexCheckpointPending",
]
