"""Compatibility alias for canonical retrieval projection infrastructure."""

import sys as _sys

from .retrieval.projection import qdrant as _canonical
from .retrieval.projection.qdrant import PAYLOAD_INDEX_SCHEMAS, QdrantIndex

__all__ = ["PAYLOAD_INDEX_SCHEMAS", "QdrantIndex"]

_sys.modules[__name__] = _canonical
