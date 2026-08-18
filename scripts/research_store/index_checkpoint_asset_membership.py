"""Compatibility alias for canonical projection checkpoint membership."""

import sys as _sys

from .retrieval.projection import index_checkpoint_asset_membership as _canonical

_sys.modules[__name__] = _canonical
