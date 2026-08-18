"""Projection namespace facade for baseline-stable checkpoint orchestration."""

from ...checkpoint_indexing_stage import (
    INDEX_CHECKPOINT_PENDING_PREFIX as INDEX_CHECKPOINT_PENDING_PREFIX,
    CheckpointIndexingStage as CheckpointIndexingStage,
    IndexCheckpointPending as IndexCheckpointPending,
)

__all__ = [
    "INDEX_CHECKPOINT_PENDING_PREFIX",
    "CheckpointIndexingStage",
    "IndexCheckpointPending",
]
