#!/usr/bin/env python3
"""Canonical public entry point for live Firecrawl smoke/fault validation."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from live_validation import (  # noqa: E402
    AuthoritativeInspector,
    Campaign,
    PROFILE_OPERATION_CAPS,
    RETIRED_SMART_OPTIONS,
    _fresearch_contract,
    main,
    parse_args,
)
from live_validation_destructive import DisposableDestructiveCampaign  # noqa: E402

__all__ = [
    "AuthoritativeInspector",
    "Campaign",
    "DisposableDestructiveCampaign",
    "PROFILE_OPERATION_CAPS",
    "RETIRED_SMART_OPTIONS",
    "_fresearch_contract",
    "main",
    "parse_args",
]


if __name__ == "__main__":
    raise SystemExit(main())
