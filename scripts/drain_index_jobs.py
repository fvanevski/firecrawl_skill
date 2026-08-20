#!/usr/bin/env python3
"""Operator launcher for the canonical index-drain implementation."""

from __future__ import annotations

import os
from pathlib import Path

from firecrawl_skill.research_store.retrieval.projection.drain import (
    CANCELLED_EXIT_CODE,
    DEFAULT_SCOPED_DEADLINE_SECONDS,
    RESUMABLE_EXIT_CODE,
    DrainCancelled,
    DrainResult,
    build_parser,
    drain_index_jobs,
    drain_index_jobs_result,
    main,
)

os.environ.setdefault(
    "FIRECRAWL_RESEARCH_DB_COMMAND",
    str(Path(__file__).resolve().with_name("research-db")),
)

__all__ = [
    "CANCELLED_EXIT_CODE",
    "DEFAULT_SCOPED_DEADLINE_SECONDS",
    "RESUMABLE_EXIT_CODE",
    "DrainCancelled",
    "DrainResult",
    "build_parser",
    "drain_index_jobs",
    "drain_index_jobs_result",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
