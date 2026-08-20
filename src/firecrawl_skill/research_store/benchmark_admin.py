"""Temporary compatibility facade for release benchmark application assembly.

Issue #265 moved authoritative benchmark CLI assembly to
``research_store.release.admin``. #269 owns removal of this legacy flat import
path.
"""

from __future__ import annotations

import sys as _sys

from .release import admin as _impl
from .release.admin import run_campaign

__all__ = ["run_campaign"]

_sys.modules[__name__] = _impl
