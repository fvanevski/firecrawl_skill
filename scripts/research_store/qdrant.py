"""Compatibility alias for canonical retrieval projection infrastructure."""

import sys as _sys

from .retrieval.projection import qdrant as _canonical

_sys.modules[__name__] = _canonical
