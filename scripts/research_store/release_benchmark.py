"""Temporary compatibility facade for measured release benchmark evaluation.

Issue #265 moved the authoritative implementation to
``research_store.release.benchmark``. #269 owns removal of this legacy flat
import path after supported callers migrate.
"""

from __future__ import annotations

import sys as _sys

from .release import benchmark as _impl
from .release.benchmark import *

_HAS_PSUTIL = _impl._HAS_PSUTIL
_HAS_PYNVML = _impl._HAS_PYNVML
_annotated_source_quality = _impl._annotated_source_quality
_canonical_match = _impl._canonical_match

_sys.modules[__name__] = _impl
