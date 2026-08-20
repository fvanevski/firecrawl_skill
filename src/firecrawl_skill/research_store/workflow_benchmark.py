"""Temporary compatibility facade for workflow benchmark evaluation.

Issue #265 moved the authoritative implementation to
``research_store.release.workflow``. #269 owns removal of this legacy flat
import path after supported callers migrate.
"""

from __future__ import annotations

import sys as _sys

from .release import workflow as _impl
from .release.workflow import *

_sys.modules[__name__] = _impl
