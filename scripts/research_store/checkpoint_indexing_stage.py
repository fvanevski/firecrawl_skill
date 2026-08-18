"""Compatibility alias for canonical projection checkpoint orchestration."""

import sys as _sys

from .retrieval.projection import checkpoint_indexing_stage as _canonical

_sys.modules[__name__] = _canonical
