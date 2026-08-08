"""Canonical research-store CLI compatibility surface.

The historical monolithic ``research_store/cli.py`` remains the implementation
for unaffected commands. Issue #221 routes ``export-run`` and ``integrity``
through the bounded, snapshot-consistent implementation here so the old audit
helpers are not reachable from supported imports or ``python -m research_store.cli``.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

from ..config import StoreConfig
from ..run_integrity_export import (
    EXPORT_RUN_SCHEMA_VERSIONS,
    INTEGRITY_SCHEMA_VERSIONS,
    build_integrity_report,
    build_run_export,
)

_LEGACY_NAME = "research_store._legacy_cli"
_LEGACY_PATH = Path(__file__).resolve().parents[1] / "cli.py"
_spec = importlib.util.spec_from_file_location(_LEGACY_NAME, _LEGACY_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover - import machinery guard
    raise ImportError(f"could not load legacy CLI from {_LEGACY_PATH}")
_legacy = importlib.util.module_from_spec(_spec)
sys.modules.setdefault(_LEGACY_NAME, _legacy)
_spec.loader.exec_module(_legacy)

# Preserve the established module-level API used throughout the test suite and
# internal callers. Explicit overrides below take precedence.
for _name in dir(_legacy):
    if not _name.startswith("__"):
        globals().setdefault(_name, getattr(_legacy, _name))


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
    _legacy._export_json(output, result)
    if command == "integrity":
        # Never duplicate the potentially sensitive artifact on stdout. The
        # file is already recursively redacted, but stdout remains a bounded
        # operational acknowledgement only.
        print(
            _legacy.dumps(
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
    return _legacy.main(resolved)


__all__ = [
    *[name for name in dir(_legacy) if not name.startswith("__")],
    "EXPORT_RUN_SCHEMA_VERSIONS",
    "INTEGRITY_SCHEMA_VERSIONS",
    "build_integrity_report",
    "build_run_export",
    "main",
]
