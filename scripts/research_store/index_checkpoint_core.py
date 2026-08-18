"""Compatibility alias for canonical projection checkpoint core."""

import sys as _sys

from .retrieval.projection import index_checkpoint_core as _canonical

_sys.modules[__name__] = _canonical
