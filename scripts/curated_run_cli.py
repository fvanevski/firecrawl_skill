#!/usr/bin/env python3
"""Explicit autonomous and curated run commands for ``frun``."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from research_store.asset_promotion_models import AssetPromotionError
from research_store.asset_promotion_service import AssetPromotionService
from research_store.container import build_run_service, build_workflow_operation_service
from research_store.curated_run_service import CuratedRunError, CuratedRunService
from research_store.workflow_service import WorkflowBoundaryError


def _json_default(value: Any) -> Any:
    if isinstance(value, (UUID, datetime, date)):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _emit(value: Any) -> None:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    print(json.dumps(value, sort_keys=True, default=_json_default))


def _service() -> CuratedRunService:
    run_service = build_run_service()
    workflow_service = build_workflow_operation_service()
    return CuratedRunService(
        run_service,
        workflow_service,
        AssetPromotionService(run_service.uow_factory),
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("objective")
    start.add_argument("--external-id")
    start.add_argument(
        "--run-mode", choices=("autonomous", "curated"), default="autonomous"
    )
    start.add_argument(
        "--mode",
        choices=("agent_led", "autonomous_local", "deterministic_debug"),
        default="autonomous_local",
        dest="execution_mode",
    )

    for name in ("mode", "prepare", "seal-acquisition", "resume"):
        command = subparsers.add_parser(name)
        command.add_argument("run_id")

    retain = subparsers.add_parser("retain")
    retain.add_argument("run_id")
    retain.add_argument("subject_id", type=UUID)
    retain.add_argument("--reason", default="operator retained curated asset")

    reject = subparsers.add_parser("reject")
    reject.add_argument("run_id")
    reject.add_argument("subject_id", type=UUID)
    reject.add_argument("--reason", required=True)

    finish = subparsers.add_parser("finish")
    finish.add_argument("run_id")
    finish.add_argument("--outcome", required=True)
    finish.add_argument("--status", choices=("complete", "failed"), default="complete")
    finish.add_argument("--source-manifest-sha256")
    finish.add_argument("--answer-sha256")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    service = _service()
    try:
        if args.command == "start":
            external_id = args.external_id or f"fr_{uuid4().hex}"
            result = service.start(
                args.objective,
                external_id,
                run_mode=args.run_mode,
                execution_mode=args.execution_mode,
            )
        elif args.command == "mode":
            result = service.status(args.run_id)
        elif args.command == "prepare":
            result = service.prepare(args.run_id)
        elif args.command == "retain":
            result = service.retain(
                args.run_id,
                args.subject_id,
                reason=args.reason,
            )
        elif args.command == "reject":
            result = service.reject(
                args.run_id,
                args.subject_id,
                reason=args.reason,
            )
        elif args.command == "seal-acquisition":
            result = service.seal_acquisition(args.run_id)
        elif args.command == "resume":
            result = service.resume(args.run_id)
        elif args.command == "finish":
            result = service.finish(
                args.run_id,
                outcome=args.outcome,
                status_name=args.status,
                source_manifest_sha256=args.source_manifest_sha256,
                answer_sha256=args.answer_sha256,
            )
        else:  # pragma: no cover - argparse constrains this branch
            raise AssertionError(args.command)
    except (
        AssetPromotionError,
        CuratedRunError,
        WorkflowBoundaryError,
        KeyError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
