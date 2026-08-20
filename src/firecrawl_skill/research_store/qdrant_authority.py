"""Compatibility alias for canonical Qdrant projection authority."""

import sys as _sys

from .retrieval.projection import authority as _canonical
from .retrieval.projection.authority import (
    capture_configured_projection_state,
    evaluate_required_alias_state,
    read_required_alias_state,
    require_configured_projection_preserved,
)

__all__ = [
    "capture_configured_projection_state",
    "evaluate_required_alias_state",
    "read_required_alias_state",
    "require_configured_projection_preserved",
]

_sys.modules[__name__] = _canonical
