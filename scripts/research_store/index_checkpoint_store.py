"""Compatibility alias for canonical projection checkpoint persistence."""

import sys as _sys

from .retrieval.projection import index_checkpoint_store as _canonical

_sys.modules[__name__] = _canonical
