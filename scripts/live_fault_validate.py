#!/usr/bin/env python3
"""Deprecated compatibility entry point for canonical live validation.

All policy and execution live in ``scripts/live_validate.py``. This name is
retained only so earlier operator notes route to the current validator instead
of preserving an independent fault runner.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from live_validate import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
