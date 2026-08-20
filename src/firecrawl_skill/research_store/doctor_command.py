from __future__ import annotations

import argparse

from .config import StoreConfig
from .doctor_diagnostics import doctor, format_human
from .export_serialization import dumps


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="research-db doctor",
        description="Report independent authoritative and infrastructure diagnostics.",
    )
    command.add_argument(
        "--human",
        action="store_true",
        help="render the same seven diagnostic domains as human-readable text",
    )
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = StoreConfig.from_env()
    checks, failed = doctor(config)
    print(format_human(checks) if args.human else dumps(checks))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
