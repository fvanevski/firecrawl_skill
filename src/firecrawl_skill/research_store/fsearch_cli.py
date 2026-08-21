"""Production CLI boundary for PostgreSQL-authoritative ``fsearch``."""

from __future__ import annotations

from collections.abc import Sequence

from .composition import build_policy_fsearch_service
from .fsearch_service import main as run_fsearch_cli


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI with dependencies supplied by the canonical composition root."""
    return run_fsearch_cli(argv, service_factory=build_policy_fsearch_service)


if __name__ == "__main__":
    raise SystemExit(main())
