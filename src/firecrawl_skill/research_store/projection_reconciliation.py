"""Compatibility alias for canonical projection reconciliation."""

import sys as _sys

from .retrieval.projection import reconciliation as _canonical
from .retrieval.projection.reconciliation import reconcile_projection_compat

__all__ = ["reconcile_projection_compat"]

_sys.modules[__name__] = _canonical
