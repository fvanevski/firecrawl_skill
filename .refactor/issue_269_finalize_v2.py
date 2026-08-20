#!/usr/bin/env python3
"""Issue #269 deterministic finalizer driver, revision 2.

This driver retires exact migration-era artifacts before the original Central
finalizer performs its import census. All architecture mappings and general
mutation helpers remain owned by ``issue_269_finalize.py``.
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
TOPOLOGY_CONTRACT_REL = "tests/contract/test_issue_269_final_topology.py"
ISSUE_216_TEST_REL = "tests/integration/test_issue_216_extraction_preflight.py"

KNOWN_REPORTING_STUB = '''"""Report-construction boundary.

``report_service.py`` has reviewed path-keyed Pyrefly debt.  Keep the current
implementation path stable in #264 and expose it through the canonical
reporting namespace; moving that debt without fixing it would invalidate the
repository baseline contract.
"""

from ..report_service import LocalSynthesisService

__all__ = ["LocalSynthesisService"]
'''

KNOWN_FIXTURE_SYMLINKS = {
    "classifier.py": Path("../classifier.py"),
    "model_gateway.py": Path("../model_gateway.py"),
}
MODEL_GATEWAY_FIXTURE_TARGET = Path("../../src/firecrawl_skill/model_gateway.py")
CLASSIFIER_FIXTURE = '''"""Deterministic classifier fixture bound to the canonical owner."""

from firecrawl_skill.research_store.acquisition.classifier import (
    PROFILES,
    classify_target,
    classify_url_type,
    main,
)

__all__ = ["PROFILES", "classify_target", "classify_url_type", "main"]
'''

ISSUE_216_LEGACY_BLOCK = '''class TestCanonicalRouting:
    def test_public_adapter_is_bounded(self):
        from firecrawl_skill.research_store import acquisition_service
        from firecrawl_skill.research_store.orchestration.composition import (
            build_production_orchestrator,
        )

        assert (
            acquisition_service.FirecrawlSearchAdapter is BoundedFirecrawlSearchAdapter
        )
        assert research_store.FirecrawlSearchAdapter is BoundedFirecrawlSearchAdapter
        # Composition root explicitly injects bounded stages
'''

ISSUE_216_FINAL_BLOCK = '''class TestCanonicalRouting:
    def test_canonical_adapter_and_composition_are_bounded(self):
        from firecrawl_skill.research_store.composition import (
            build_production_orchestrator,
        )

        assert (
            BoundedFirecrawlSearchAdapter.__module__
            == "firecrawl_skill.research_store.acquisition.adapters.bounded_firecrawl"
        )
        assert not hasattr(research_store, "FirecrawlSearchAdapter")
        # Composition root explicitly injects bounded stages
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


def _require_known_fixture_symlinks(core: ModuleType) -> dict[str, Path]:
    fixture_dir = core.SCRIPTS / "fixtures"
    verified: dict[str, Path] = {}
    for name, expected_target in KNOWN_FIXTURE_SYMLINKS.items():
        path = fixture_dir / name
        if not path.is_symlink():
            raise RuntimeError(
                f"{path.relative_to(core.ROOT)} is not the expected tracked symlink"
            )
        actual_target = path.readlink()
        if actual_target != expected_target:
            raise RuntimeError(
                f"{path.relative_to(core.ROOT)} points to {actual_target}, expected "
                f"{expected_target}; refusing to infer a different fixture state"
            )
        if not path.is_file():
            raise RuntimeError(
                f"{path.relative_to(core.ROOT)} target is missing before migration"
            )
        verified[name] = path
    return verified


def _require_known_test_contracts(core: ModuleType) -> tuple[Path, Path]:
    topology = core.ROOT / TOPOLOGY_CONTRACT_REL
    issue_216 = core.ROOT / ISSUE_216_TEST_REL
    topology_source = topology.read_text(encoding="utf-8")
    for forbidden in (
        "firecrawl_skill.research_store.service",
        "firecrawl_skill.research_store.acquisition_service",
        "firecrawl_skill.research_store.direct_scrape_service",
        "firecrawl_skill.research_store.acquisition.direct_scrape",
    ):
        if f'    "{forbidden}",' not in topology_source:
            raise RuntimeError(
                f"{TOPOLOGY_CONTRACT_REL} no longer contains expected forbidden-name "
                f"assertion data for {forbidden}; refusing to infer a new contract"
            )
    issue_216_source = issue_216.read_text(encoding="utf-8")
    if ISSUE_216_LEGACY_BLOCK not in issue_216_source:
        raise RuntimeError(
            f"{ISSUE_216_TEST_REL} no longer contains the exact reviewed legacy "
            "routing-test block; refusing an ambiguous test migration"
        )
    return topology, issue_216


def _migrate_fixture_symlinks(core: ModuleType, fixtures: dict[str, Path]) -> list[str]:
    classifier = fixtures["classifier.py"]
    classifier.unlink()
    classifier.write_text(CLASSIFIER_FIXTURE, encoding="utf-8")

    model_gateway = fixtures["model_gateway.py"]
    model_gateway.unlink()
    model_gateway.symlink_to(MODEL_GATEWAY_FIXTURE_TARGET)
    canonical_model_gateway = core.SRC / "model_gateway.py"
    if not model_gateway.is_file():
        raise RuntimeError("canonical model-gateway fixture symlink is dangling")
    if model_gateway.resolve() != canonical_model_gateway.resolve():
        raise RuntimeError(
            "model-gateway fixture did not resolve to src/firecrawl_skill/model_gateway.py"
        )

    return [
        classifier.relative_to(core.ROOT).as_posix(),
        model_gateway.relative_to(core.ROOT).as_posix(),
    ]


def _migrate_issue_216_test(issue_216: Path) -> None:
    source = issue_216.read_text(encoding="utf-8")
    if source.count(ISSUE_216_LEGACY_BLOCK) != 1:
        raise RuntimeError(
            f"{ISSUE_216_TEST_REL} legacy routing block is not uniquely identifiable"
        )
    issue_216.write_text(
        source.replace(ISSUE_216_LEGACY_BLOCK, ISSUE_216_FINAL_BLOCK, 1),
        encoding="utf-8",
    )


def _filter_topology_assertion_data_violations(
    violations: list[str],
) -> list[str]:
    prefix = f"legacy dynamic target: {TOPOLOGY_CONTRACT_REL} -> "
    return [violation for violation in violations if not violation.startswith(prefix)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    core = _load_core()
    core._require_clean_exact_head(args.expected_head)
    target_path = _require_known_report_stub(core)
    fixtures = _require_known_fixture_symlinks(core)
    topology_contract, issue_216_test = _require_known_test_contracts(core)

    if not args.apply:
        print(
            json.dumps(
                {
                    "status": "ready",
                    "head": args.expected_head,
                    "forbidden_paths": len(core.FORBIDDEN_PATHS),
                    "known_reporting_stub_verified": True,
                    "known_fixture_symlinks_verified": sorted(
                        path.relative_to(core.ROOT).as_posix()
                        for path in fixtures.values()
                    ),
                    "topology_assertion_data_verified": True,
                    "issue_216_legacy_contract_verified": True,
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

    # The original fixture links point at top-level implementations that #269
    # deletes. Migrate them before the rewrite census so no pass traverses an
    # obsolete target and final verification cannot encounter dangling links.
    migrated_fixtures = _migrate_fixture_symlinks(core, fixtures)

    # Issue #216 carried assertions for migration-only adapter aliases that #269
    # intentionally removes. Replace only the exact reviewed block with the
    # final-state canonical ownership assertions before the generic import pass.
    _migrate_issue_216_test(issue_216_test)

    changed_import_files = 0
    changed_target_files = 0
    for path in core._python_targets():
        changed_import_files += int(core._apply_import_rewrites(path))
    for path in core._python_targets():
        # This contract deliberately enumerates forbidden module identities as
        # assertion data. Mutating those literals would weaken the test itself.
        if path == topology_contract:
            continue
        changed_target_files += int(core._rewrite_string_targets(path))

    core._rewrite_domain_codec()
    core._move_report_construction()
    core._remove_package_aliases()
    core._clean_workflow_paths()
    deleted = core._delete_obsolete_paths()
    baseline_removed = core._prune_deleted_baseline_paths()

    # The core verifier scans every string constant because ordinary tests may
    # contain monkeypatch/import targets. The final-topology contract is the one
    # reviewed exception: its forbidden-name strings are assertion data. Real
    # imports in that file remain checked by the core verifier and are not
    # filtered here.
    violations = _filter_topology_assertion_data_violations(core._verify_final_state())
    summary = {
        "status": "failed" if violations else "finalized",
        "exact_head_before_mutation": args.expected_head,
        "known_reporting_stub_removed": True,
        "migrated_fixture_shims": migrated_fixtures,
        "issue_216_final_contract_rewritten": True,
        "topology_assertion_data_preserved": True,
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
