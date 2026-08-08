"""Canonical research-store CLI compatibility surface.

The historical monolithic ``research_store/cli.py`` is executed into this
canonical module namespace so existing imports, monkeypatches, and command
helpers retain their established module-global seams. Issue #221 then replaces
only ``export-run`` and ``integrity`` with the bounded snapshot implementation.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Execute the established implementation in this module's globals. Keeping
# ``__package__`` at ``research_store`` preserves the relative imports used by
# legacy function bodies at call time, while this package remains the import
# target ``research_store.cli`` and continues to support ``python -m``.
_LEGACY_PATH = Path(__file__).resolve().parents[1] / "cli.py"
__package__ = "research_store"
exec(compile(_LEGACY_PATH.read_text(encoding="utf-8"), str(_LEGACY_PATH), "exec"), globals())
_legacy_main = main
_legacy_export_json = _export_json

from research_store.config import StoreConfig
from research_store.run_integrity_export import (
    EXPORT_RUN_SCHEMA_VERSIONS,
    INTEGRITY_SCHEMA_VERSIONS,
    build_integrity_report,
    build_run_export,
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
            dumps(
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


def main(argv: list[str] | None = None) -> int:
    resolved = list(sys.argv[1:] if argv is None else argv)
    if resolved and resolved[0] in {"export-run", "integrity"}:
        return _artifact_main(resolved[0], resolved[1:])
    return _legacy_main(resolved)
