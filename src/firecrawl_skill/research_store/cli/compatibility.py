"""Compatibility command family for canonical CLI overlay contracts.

These commands intentionally use raw argv because the canonical package overlays
stricter/current grammar on top of the historical parser definitions.  Keeping
that compatibility boundary in one family preserves the public CLI contract
without retaining command-specific execution in the package root.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

COMMANDS = {"export-run", "integrity", "index-reconcile", "reconcile-qdrant"}


def _artifact_parser(command: str, deps) -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog=f"research-db {command}")
    root.add_argument("id")
    root.add_argument("--output", required=True)
    if command == "export-run":
        root.add_argument(
            "--schema-version",
            choices=deps.EXPORT_RUN_SCHEMA_VERSIONS,
            default="export-run-v2",
        )
    else:
        root.add_argument(
            "--schema-version",
            choices=deps.INTEGRITY_SCHEMA_VERSIONS,
            default="integrity-v1",
        )
    return root


def _run_artifact(command: str, argv: list[str], deps) -> int:
    args = _artifact_parser(command, deps).parse_args(argv)
    config = deps.StoreConfig.from_env()
    if command == "export-run":
        result = deps.build_run_export(config, args.id, args.schema_version)
    else:
        result = deps.build_integrity_report(config, args.id, args.schema_version)
    output = Path(args.output)
    deps._export_json(output, result)
    if command == "integrity":
        print(
            deps.dumps(
                {
                    "status": "written",
                    "path": str(output),
                    "schema_version": result["schema_version"],
                    "integrity_status": result["diagnostics"]["overall_status"],
                    "run_id": str(result["run"]["id"]),
                }
            )
        )
    return 0


def _reconcile_parser(command: str) -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog=f"research-db {command}")
    root.add_argument(
        "run",
        nargs="?",
        help=(
            "research run UUID or external_run_id; when omitted, report only "
            "current projection-wide compatibility (no historical run claim)"
        ),
    )
    root.add_argument(
        "--repair",
        action="store_true",
        help=(
            "explicitly perform bounded projection repairs (requeue exact chunks, "
            "delete PostgreSQL-orphaned points, create missing payload indexes)"
        ),
    )
    return root


def _run_reconcile(command: str, argv: list[str], deps) -> int:
    args = _reconcile_parser(command).parse_args(argv)
    config = deps.StoreConfig.from_env()
    try:
        if args.run:
            result = deps.reconcile_run(config, args.run, repair=args.repair)
        else:
            result = deps.reconcile_projection_compat(
                config,
                repair=args.repair,
                index_build=deps._index_build,
            )
    except deps.ReconciliationError as exc:
        print(
            deps.dumps(
                {
                    "schema_version": "qdrant-reconciliation-v2",
                    "ok": False,
                    "scope": "run" if args.run else "projection",
                    "error": str(exc),
                }
            ),
            file=sys.stderr,
        )
        return 2
    print(deps.dumps(result))
    final = result.get("post_repair") or result
    return 0 if final.get("ok", False) else 1


def run_argv(command: str, argv: list[str], deps) -> int:
    """Parse and execute one canonical compatibility-overlay command."""
    if command in {"export-run", "integrity"}:
        return _run_artifact(command, argv, deps)
    if command in {"index-reconcile", "reconcile-qdrant"}:
        return _run_reconcile(command, argv, deps)
    raise AssertionError(command)
