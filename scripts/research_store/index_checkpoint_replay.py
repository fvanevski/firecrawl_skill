"""Compatibility alias for canonical projection checkpoint replay."""

import sys as _sys

from .retrieval.projection import index_checkpoint_replay as _canonical

_sys.modules[__name__] = _canonical
