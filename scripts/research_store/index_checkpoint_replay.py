"""Compatibility alias for canonical projection checkpoint replay."""

import sys as _sys

from .retrieval.projection import index_checkpoint_replay as _canonical
from .retrieval.projection.index_checkpoint_replay import replay_completed_checkpoint

__all__ = ["replay_completed_checkpoint"]

_sys.modules[__name__] = _canonical
