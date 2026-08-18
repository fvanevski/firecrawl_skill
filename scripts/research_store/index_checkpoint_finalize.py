"""Compatibility alias for canonical projection checkpoint finalization."""

import sys as _sys

from .retrieval.projection import index_checkpoint_finalize as _canonical

_sys.modules[__name__] = _canonical
