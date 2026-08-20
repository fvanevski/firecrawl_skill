"""Legacy launch facade for the canonical :mod:`firecrawl_skill.research_store.cli` package.

The package is the implementation authority.  This file remains only so direct
legacy launches of ``src/firecrawl_skill/research_store/cli.py`` continue to
delegate to the same parser and dispatcher.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from firecrawl_skill.research_store.cli import main, parser

__all__ = ["main", "parser"]


if __name__ == "__main__":
    raise SystemExit(main())
