"""Compatibility alias for canonical retrieval projection indexing."""

import sys as _sys

from .retrieval.projection import indexing as _canonical

_sys.modules[__name__] = _canonical
