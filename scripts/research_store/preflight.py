"""Temporary compatibility facade for release preflight.

Issue #265 moved the authoritative implementation to
``research_store.release.preflight``. #269 owns removal of this legacy flat
import path after supported callers migrate.
"""

from __future__ import annotations

import sys as _sys

from .release import preflight as _impl
from .release.preflight import (
    probe_embedding,
    probe_firecrawl,
    probe_generative,
    probe_index_worker,
    probe_postgres,
    probe_qdrant,
    probe_reranker,
    probe_resources,
    probe_valkey,
    run_complete_preflight,
)

__all__ = [
    "probe_embedding",
    "probe_firecrawl",
    "probe_generative",
    "probe_index_worker",
    "probe_postgres",
    "probe_qdrant",
    "probe_reranker",
    "probe_resources",
    "probe_valkey",
    "run_complete_preflight",
]

_sys.modules[__name__] = _impl
