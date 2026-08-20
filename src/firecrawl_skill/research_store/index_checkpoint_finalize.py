"""Compatibility import for canonical retrieval projection checkpoint finalization."""

from .retrieval.projection.index_checkpoint_finalize import (
    _IndexCheckpointFinalizeMixin,
)

__all__ = ["_IndexCheckpointFinalizeMixin"]
