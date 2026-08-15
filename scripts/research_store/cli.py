"""Legacy launch facade for the canonical :mod:`research_store.cli` package.

The package is the implementation authority.  This file remains only so direct
legacy launches of ``scripts/research_store/cli.py`` continue to delegate to the
same parser and dispatcher.
"""

from __future__ import annotations

from research_store.cli import main, parser

__all__ = ["main", "parser"]


if __name__ == "__main__":
    raise SystemExit(main())
