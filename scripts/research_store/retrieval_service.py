"""Compatibility alias for the canonical retrieval application service."""

import sys as _sys

from .retrieval import service as _canonical
from .retrieval.service import RetrievalService

__all__ = ["RetrievalService"]

_sys.modules[__name__] = _canonical
