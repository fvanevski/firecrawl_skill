"""Canonical research-store CLI compatibility surface.

The historical monolithic ``research_store/cli.py`` is executed into this
canonical module namespace so existing imports, monkeypatches, and command
helpers retain their established module-global seams.  Corrective command
implementations are overlaid here when their contracts require stronger
PostgreSQL authority than the historical parser can express.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Preserve the historical module path while executing its code in this package
# namespace. Legacy helpers derive the repository root from ``__file__`` at call
# time, so retaining cli.py here is part of the compatibility contract.
_PACKAGE_INIT_PATH = Path(__file__).resolve()
_LEGACY_PATH = _PACKAGE_INIT_PATH.parents[1] / "cli.py"
__package__ = "research_store"
__file__ = str(_LEGACY_PATH)
exec(  # noqa: S102 - compatibility loader for this repository-owned module
    compile(_LEGACY_PATH.read_text(encoding="utf-8"), str(_LEGACY_PATH), "exec"),
    globals(),
)
_legacy_main = globals()["main"]
_legacy_export_json = globals()["_export_json"]
_legacy_dumps = globals()["dumps"]
_legacy_index_build = globals()["_index_build"]

from research_store.config import StoreConfig
from research_store.projection_reconciliation import reconcile_projection_compat
from research_store.qdrant import PAYLOAD_INDEX_SCHEMAS, QdrantIndex
from research_store.reconciliation import ReconciliationError, reconcile_run
from research_store.run_integrity_export import (
    EXPORT_RUN_SCHEMA_VERSIONS,
    INTEGRITY_SCHEMA_VERSIONS,
    build_integrity_report,
    build_run_export,
)


def _index_build(config, document_id=None, *, repair_orphans=False):
    """Legacy build plus typed payload-index provisioning.

    ``index-build`` is an explicit write path, so this is the appropriate place
    to create missing Qdrant payload indexes. Read-only reconciliation never
    calls this wrapper unless ``--repair`` was explicitly requested.
    """
    result = _legacy_index_build(
        config,
        document_id,
        repair_orphans=repair_orphans,
    )
    definition = result["index_definition"]
    index = QdrantIndex(
        config.qdrant_url,
        config.qdrant_api_key,
        definition["physical_collection"],
        definition["dimension"],
        definition["distance_metric"],
    )
    result["payload_indexes"] = index.ensure_payload_indexes(
        PAYLOAD_INDEX_SCHEMAS,
        create_missing=True,
    )
    return result


def _index_reconcile(config, repair=False):
    """Projection-wide compatibility seam for legacy imports and ``doctor``.

    This scope is deliberately not presented as historical run provenance. The
    first-class CLI command accepts a run identifier and uses the immutable
    checkpoint/seal authority in :mod:`research_store.reconciliation`.
    """
    return reconcile_projection_compat(
        config,
        repair=repair,
        index_build=_index_build,
    )


def _artifact_parser(command: str) -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog=f"research-db {command}")
    root.add_argument("id")
    root.add_argument("--output", required=True)
    if command == "export-run":
        root.add_argument(
            "--schema-version",
            choices=EXPORT_RUN_SCHEMA_VERSIONS,
            default="export-run-v2",
        )
    else:
        root.add_argument(
            "--schema-version",
            choices=INTEGRITY_SCHEMA_VERSIONS,
            default="integrity-v1",
        )
    return root


def _artifact_main(command: str, argv: list[str]) -> int:
    args = _artifact_parser(command).parse_args(argv)
    config = StoreConfig.from_env()
    if command == "export-run":
        result = build_run_export(config, args.id, args.schema_version)
    else:
        result = build_integrity_report(config, args.id, args.schema_version)
    output = Path(args.output)
    _legacy_export_json(output, result)
    if command == "integrity":
        print(
            _legacy_dumps(
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


def _reconcile_main(command: str, argv: list[str]) -> int:
    args = _reconcile_parser(command).parse_args(argv)
    config = StoreConfig.from_env()
    try:
        if args.run:
            result = reconcile_run(config, args.run, repair=args.repair)
        else:
            result = reconcile_projection_compat(
                config,
                repair=args.repair,
                index_build=_index_build,
            )
    except ReconciliationError as exc:
        print(
            _legacy_dumps(
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
    print(_legacy_dumps(result))
    final = result.get("post_repair") or result
    return 0 if final.get("ok", False) else 1


def main(argv: list[str] | None = None) -> int:
    resolved = list(sys.argv[1:] if argv is None else argv)
    if resolved and resolved[0] in {"export-run", "integrity"}:
        return _artifact_main(resolved[0], resolved[1:])
    if resolved and resolved[0] in {"index-reconcile", "reconcile-qdrant"}:
        return _reconcile_main(resolved[0], resolved[1:])
    return _legacy_main(resolved)
