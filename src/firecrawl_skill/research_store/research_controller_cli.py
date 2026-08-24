"""Agent-facing CLI for the canonical deterministic research controller."""

from __future__ import annotations

import argparse
import json
from typing import Any

from .research_controller_contract import (
    DISPOSITION_BLOCKED,
    DISPOSITION_CANCELLED,
    DISPOSITION_CONTINUE,
    DISPOSITION_FAILED,
    DISPOSITION_OPERATOR,
)

_RESUMABLE_EXIT = 75


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="start and advance one research run")
    run.add_argument("objective", nargs="+")
    run.add_argument(
        "--retained-only",
        action="store_true",
        help="forbid provider acquisition for this run",
    )

    for name in ("continue", "status", "result"):
        command = subparsers.add_parser(name)
        command.add_argument("run_id")

    return parser


def _emit(value: Any) -> dict[str, Any]:
    payload = value.to_dict() if hasattr(value, "to_dict") else dict(value)
    print(json.dumps(payload, sort_keys=True, default=str))
    return payload


def _exit_code(payload: dict[str, Any]) -> int:
    disposition = str(payload.get("disposition") or "")
    if disposition in {DISPOSITION_FAILED, DISPOSITION_CANCELLED}:
        return 1
    if disposition in {
        DISPOSITION_BLOCKED,
        DISPOSITION_OPERATOR,
        DISPOSITION_CONTINUE,
    }:
        return _RESUMABLE_EXIT
    return 0


def main(argv: list[str] | None = None) -> int:
    from .research_controller import build_research_controller

    parser = build_parser()
    args = parser.parse_args(argv)
    controller = build_research_controller()
    try:
        if args.command == "run":
            value = controller.run(
                " ".join(args.objective),
                retained_only=bool(args.retained_only),
            )
        elif args.command == "continue":
            value = controller.continue_run(args.run_id)
        elif args.command == "status":
            value = controller.status(args.run_id)
        elif args.command == "result":
            value = controller.result(args.run_id)
        else:  # pragma: no cover - argparse enforces the command set.
            raise AssertionError(args.command)
    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    payload = _emit(value)
    return _exit_code(payload)


__all__ = ["build_parser", "main"]
