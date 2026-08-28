"""Regression contract for issue #268 test topology."""

from __future__ import annotations

import ast
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
RELOCATION_TOMBSTONE_MARKER = "Temporary #291 relocation tombstone."
SUPERSEDED_CONTRACT_MARKER = "Superseded contract tombstone."
RELOCATIONS = {
    "tests/unit/test_asset_promotion_integration.py": "tests/integration/test_asset_promotion_integration.py",
    "tests/unit/test_asset_promotion_reopen_concurrency.py": "tests/integration/test_asset_promotion_reopen_concurrency.py",
    "tests/unit/test_asset_promotion_migration_compat.py": "tests/integration/test_asset_promotion_migration_compat.py",
    "tests/unit/test_curated_run_integration.py": "tests/integration/test_curated_run_integration.py",
    "tests/unit/test_issue_215_completion_budget.py": "tests/integration/test_issue_215_completion_budget.py",
}
SUPERSEDED_CONTRACTS = {
    "tests/contract/test_release_invariants.py": "tests/contract/test_release_invariant_contracts.py",
}
SUPERSEDED_RELEASE_AUTHORITIES = {
    "tests/contract/test_release_invariant_contracts.py": {
        "test_agent_led_without_supplier_hard_fails",
        "test_agent_led_uses_host_authority_and_never_local_model",
        "test_not_invoked_tokens_are_not_applicable_and_can_satisfy_release",
        "test_missing_semantic_usage_is_incomplete_and_forces_no_go",
        "test_embedding_batch_failure_is_incomplete_and_forces_no_go",
        "test_completed_metrics_with_run_errors_force_no_go",
        "test_unavailable_resource_collectors_are_null_with_explicit_reason",
        "test_partial_resource_window_is_incomplete",
        "test_go_with_conditions_returns_nonzero_strict_cli_exit",
        "test_basename_collision_does_not_false_match",
        "test_valkey_unavailable_fails_complete_preflight",
        "test_candidate_sha_mismatch_fails_complete_preflight",
    },
    "tests/integration/test_claims_evidence.py": {
        "test_citation_pass_validation_overrides_existing_evidence_link",
    },
    "tests/integration/test_performance_telemetry_integration.py": {
        "test_overlapping_events_remain_exactly_run_scoped",
    },
}


def _is_active_test(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    return not any(
        marker in source
        for marker in (RELOCATION_TOMBSTONE_MARKER, SUPERSEDED_CONTRACT_MARKER)
    )


def _function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_behavior_boundary_distribution_uses_only_canonical_roots() -> None:
    active = [path for path in TESTS.rglob("test_*.py") if _is_active_test(path)]
    counts = Counter(path.relative_to(TESTS).parts[0] for path in active)
    expected_roots = {"unit", "integration", "contract", "acceptance"}

    assert active
    assert set(counts) == expected_roots
    assert all(counts[root] > 0 for root in expected_roots)
    assert all(path.relative_to(TESTS).parts[0] in expected_roots for path in active)


def test_database_backed_suites_have_canonical_integration_ownership() -> None:
    for old_path, new_path in RELOCATIONS.items():
        old = ROOT / old_path
        new = ROOT / new_path
        assert new.is_file(), new_path
        assert RELOCATION_TOMBSTONE_MARKER not in new.read_text(encoding="utf-8")
        if old.exists():
            source = old.read_text(encoding="utf-8")
            assert RELOCATION_TOMBSTONE_MARKER in source
            assert "def test_" not in source


def test_central_test_authority_uses_only_canonical_integration_paths() -> None:
    scripts = ROOT / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        from ci_authority import resolved_membership

        head = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip()
        membership, _ = resolved_membership(ROOT, head_sha=head)
    finally:
        sys.path.remove(str(scripts))

    selected_paths = {
        selector.base_path
        for selectors in membership.values()
        for selector in selectors
    }
    for old_path, new_path in RELOCATIONS.items():
        assert old_path not in selected_paths
        assert new_path in selected_paths


def test_superseded_contracts_are_test_free_and_have_canonical_owners() -> None:
    for old_path, new_path in SUPERSEDED_CONTRACTS.items():
        old = ROOT / old_path
        new = ROOT / new_path
        assert old.is_file(), old_path
        assert new.is_file(), new_path
        source = old.read_text(encoding="utf-8")
        assert SUPERSEDED_CONTRACT_MARKER in source
        assert "pytest.mark.skip" not in source
        tree = ast.parse(source, filename=old_path)
        assert not any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
            for node in ast.walk(tree)
        )

    for authority_path, expected_tests in SUPERSEDED_RELEASE_AUTHORITIES.items():
        authority = ROOT / authority_path
        assert authority.is_file(), authority_path
        assert expected_tests <= _function_names(authority), authority_path
