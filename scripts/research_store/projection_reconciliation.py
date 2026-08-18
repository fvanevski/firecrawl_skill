"""Compatibility alias for canonical projection reconciliation."""

import sys as _sys

from .retrieval.projection import reconciliation as _canonical

_sys.modules[__name__] = _canonical
