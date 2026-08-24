"""Deprecated direct entry point for candidate-budget operations."""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "ERROR: candidate_budget_cli.py is not an executable operator surface; "
        "use scripts/candidate-budget so research-env provenance is enforced",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
