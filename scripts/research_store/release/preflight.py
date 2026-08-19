"""Canonical release-preflight boundary for issue #265.

``research_store.preflight`` carries reviewed path-keyed Pyrefly debt. Keep
that implementation path stable rather than laundering the baseline; this
module provides the canonical release namespace until #269 removes the
compatibility scaffolding.
"""

from __future__ import annotations

import sys as _sys

from .. import preflight as _impl
from ..preflight import (
    _config,
    _probe_writable_directory,
    _redact_url_credentials,
    _worker_heartbeat_is_fresh,
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
