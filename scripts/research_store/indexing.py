"""Compatibility alias for canonical retrieval projection indexing."""

import sys as _sys

from .retrieval.projection import indexing as _canonical
from .retrieval.projection.indexing import IndexWorker, LeaseLost, OpenAICompatibleEmbedder

__all__ = ["IndexWorker", "LeaseLost", "OpenAICompatibleEmbedder"]

_sys.modules[__name__] = _canonical
