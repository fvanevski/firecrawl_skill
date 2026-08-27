"""Agent-facing CLI for the canonical deterministic research controller."""

from __future__ import annotations

import argparse
import json
from typing import Any
from uuid import UUID

from .research_controller_contract import (
    DELIVERY_HOST_HANDOFF,
    DELIVERY_MODES,
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
    run.add_argument(
        "--curated",
        action="store_true",
        help="require one durable human evidence-selection action before completion",
    )
    run.add_argument(
        "--delivery-mode",
        choices=tuple(sorted(DELIVERY_MODES)),
        default=DELIVERY_HOST_HANDOFF,
        help="terminal delivery contract (default: host_handoff)",
    )

    for name in ("continue", "status", "result"):
        command = subparsers.add_parser(name)
        command.add_argument("run_id")

    action = subparsers.add_parser("action", help="inspect one public operator action")
    action.add_argument("action_id")

    approve = subparsers.add_parser("approve", help="approve one soft policy action")
    approve.add_argument("action_id")
    approve.add_argument("--reason", required=True)
    approve.add_argument("--authorized-by", required=True)

    fork = subparsers.add_parser("fork", help="fork a material scope change")
    fork.add_argument("action_id")
    fork.add_argument("revised_objective", nargs="+")
    fork.add_argument("--reason", required=True)
    fork.add_argument("--authorized-by", required=True)

    curate = subparsers.add_parser(
        "curate", help="submit one complete curated selection"
    )
    curate.add_argument("action_id")
    curate.add_argument("--retain", action="append", required=True)
    curate.add_argument("--reject-rest", action="store_true", required=True)
    curate.add_argument("--reason", required=True)
    curate.add_argument("--authorized-by", required=True)

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
                curated=bool(args.curated),
                delivery_mode=args.delivery_mode,
            )
        elif args.command == "continue":
            value = controller.continue_run(args.run_id)
        elif args.command == "status":
            value = controller.status(args.run_id)
        elif args.command == "result":
            value = controller.result(args.run_id)
        elif args.command == "action":
            value = controller.action(args.action_id)
        elif args.command == "approve":
            value = controller.approve(
                args.action_id,
                reason=args.reason,
                authorized_by=args.authorized_by,
            )
        elif args.command == "fork":
            value = controller.fork(
                args.action_id,
                " ".join(args.revised_objective),
                reason=args.reason,
                authorized_by=args.authorized_by,
            )
        elif args.command == "curate":
            value = controller.curate(
                args.action_id,
                retain_subject_ids=[UUID(value) for value in args.retain],
                reject_rest=bool(args.reject_rest),
                reason=args.reason,
                authorized_by=args.authorized_by,
            )
        else:  # pragma: no cover - argparse enforces the command set.
            raise AssertionError(args.command)
    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    payload = _emit(value)
    return _exit_code(payload)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
