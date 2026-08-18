"""Compatibility alias for canonical projection checkpoint service."""

import sys as _sys

from .retrieval.projection import index_checkpoint_service as _canonical

_sys.modules[__name__] = _canonical
