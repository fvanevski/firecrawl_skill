"""Compatibility alias for canonical projection checkpoint models."""

import sys as _sys

from .retrieval.projection import index_checkpoint_models as _canonical

_sys.modules[__name__] = _canonical
