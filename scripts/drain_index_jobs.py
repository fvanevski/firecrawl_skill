#!/usr/bin/env python3
"""Operator launcher for the canonical index-drain implementation."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault(
    "FIRECRAWL_RESEARCH_DB_COMMAND",
    str(Path(__file__).resolve().with_name("research-db")),
)

from firecrawl_skill.research_store.retrieval.projection.drain import main


if __name__ == "__main__":
    raise SystemExit(main())
