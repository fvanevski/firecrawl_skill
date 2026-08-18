"""Compatibility alias for canonical projection checkpoint service."""

import sys as _sys

from .retrieval.projection import index_checkpoint_service as _canonical
from .retrieval.projection.index_checkpoint_service import IndexCheckpointService

__all__ = ["IndexCheckpointService"]

_sys.modules[__name__] = _canonical
