"""Compatibility alias for canonical projection checkpoint models."""

import sys as _sys

from .retrieval.projection import index_checkpoint_models as _canonical
from .retrieval.projection.index_checkpoint_models import (
    IRRECOVERABLE_CLASSES,
    RECOVERABLE_CLASSES,
    IndexCheckpoint,
    IndexCheckpointError,
    IndexCheckpointStaleError,
    IndexFinalization,
    _checkpoint_from_row,
    _iso,
    _membership_digest,
    _parse_datetime,
    _required_datetime,
)

__all__ = [
    "IRRECOVERABLE_CLASSES",
    "RECOVERABLE_CLASSES",
    "IndexCheckpoint",
    "IndexCheckpointError",
    "IndexCheckpointStaleError",
    "IndexFinalization",
]

_sys.modules[__name__] = _canonical
