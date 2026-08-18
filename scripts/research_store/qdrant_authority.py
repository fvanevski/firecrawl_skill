"""Compatibility alias for canonical Qdrant projection authority."""

import sys as _sys

from .retrieval.projection import authority as _canonical

_sys.modules[__name__] = _canonical
