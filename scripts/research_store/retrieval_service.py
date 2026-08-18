"""Compatibility alias for the canonical retrieval application service."""

import sys as _sys

from .retrieval import service as _canonical

_sys.modules[__name__] = _canonical
