#!/usr/bin/env python3
"""Issue #269 deterministic finalizer driver, revision 2.

This driver exists only to retire the exact P5-era
``reporting/construction.py`` re-export stub before the original Central
finalizer performs its import census. All architecture mappings and mutation
helpers remain owned by ``issue_269_finalize.py``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

SELF = Path(__file__).resolve()
CORE_PATH = SELF.with_name("issue_269_finalize.py")

KNOWN_REPORTING_STUB = '''"""Report-construction boundary.

``report_service.py`` has reviewed path-keyed Pyrefly debt.  Keep the current
implementation path stable in #264 and expose it through the canonical
reporting namespace; moving that debt without fixing it would invalidate the
repository baseline contract.
"""

from ..report_service import LocalSynthesisService

__all__ = ["LocalSynthesisService"]
'''


def _load_core() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_issue_269_finalize_core", CORE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Central finalizer core: {CORE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _require_known_report_stub(core: ModuleType) -> Path:
    source_path = core.STORE / "report_service.py"
    target_path = core.STORE / "reporting" / "construction.py"
    if not source_path.is_file():
        raise RuntimeError(
            "report_service.py is required for the prescribed physical move"
        )
    if not target_path.is_file():
        raise RuntimeError(
            "expected P5 reporting/construction.py stub is missing; refusing "
            "to infer a different migration state"
        )
    actual = target_path.read_text(encoding="utf-8")
    if actual != KNOWN_REPORTING_STUB:
        raise RuntimeError(
            "reporting/construction.py does not match the exact reviewed P5 "
            "re-export stub; refusing to overwrite an unknown target"
        )
    return target_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    core = _load_core()
    core._require_clean_exact_head(args.expected_head)
    target_path = _require_known_report_stub(core)

    if not args.apply:
        print(
            json.dumps(
                {
                    "status": "ready",
                    "head": args.expected_head,
                    "forbidden_paths": len(core.FORBIDDEN_PATHS),
                    "known_reporting_stub_verified": True,
                    "message": (
                        "rerun issue_269_finalize_v2.py with --apply to execute "
                        "the Central-owned migration"
                    ),
                },
                indent=2,
            )
        )
        return 0

    # The original helper's AST rewrite would rewrite the P5 stub into a
    # self-import before _move_report_construction() runs. Remove only the exact
    # reviewed stub first so the canonical implementation can be moved into its
    # final owner without weakening the move guard for any unknown target.
    target_path.unlink()

    changed_import_files = 0
    changed_target_files = 0
    for path in core._python_targets():
        changed_import_files += int(core._apply_import_rewrites(path))
    for path in core._python_targets():
        changed_target_files += int(core._rewrite_string_targets(path))

    core._rewrite_domain_codec()
    core._move_report_construction()
    core._remove_package_aliases()
    core._clean_workflow_paths()
    deleted = core._delete_obsolete_paths()
    baseline_removed = core._prune_deleted_baseline_paths()

    violations = core._verify_final_state()
    summary = {
        "status": "failed" if violations else "finalized",
        "exact_head_before_mutation": args.expected_head,
        "known_reporting_stub_removed": True,
        "import_rewrite_files": changed_import_files,
        "dynamic_target_rewrite_files": changed_target_files,
        "deleted_paths": deleted,
        "deleted_pyrefly_baseline_records": baseline_removed,
        "violations": violations,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    if violations:
        return 1

    # Both files are temporary migration machinery. Retire them only after the
    # final-state verification succeeds so `git add -A` records both deletions
    # in the same local mechanical commit.
    if core.SELF.exists():
        core.SELF.unlink()
    SELF.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
