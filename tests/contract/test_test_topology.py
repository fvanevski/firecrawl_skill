"""Regression contract for issue #268 test topology."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"
TOMBSTONE_MARKER = "Temporary #291 relocation tombstone."
RELOCATIONS = {
    "tests/unit/test_asset_promotion_integration.py": "tests/integration/test_asset_promotion_integration.py",
    "tests/unit/test_asset_promotion_reopen_concurrency.py": "tests/integration/test_asset_promotion_reopen_concurrency.py",
    "tests/unit/test_asset_promotion_migration_compat.py": "tests/integration/test_asset_promotion_migration_compat.py",
    "tests/unit/test_curated_run_integration.py": "tests/integration/test_curated_run_integration.py",
    "tests/unit/test_issue_215_completion_budget.py": "tests/integration/test_issue_215_completion_budget.py",
}
ACTIVE_CONSUMERS = (
    ROOT / ".github/workflows/index-checkpoint.yml",
    ROOT / ".github/workflows/authoritative-fsearch.yml",
    ROOT / "references/audit-remediation-release-gates.json",
    ROOT / "references/migration-guide.md",
    ROOT / "tests/contract/test_asset_promotion_contract.py",
    ROOT / "tests/unit/test_curated_run_lifecycle.py",
)


def _is_active_test(path: Path) -> bool:
    return TOMBSTONE_MARKER not in path.read_text(encoding="utf-8")


def test_behavior_boundary_distribution_is_exact() -> None:
    active = [path for path in TESTS.rglob("test_*.py") if _is_active_test(path)]
    counts = Counter(path.relative_to(TESTS).parts[0] for path in active)
    assert len(active) == 135
    assert counts == {
        "unit": 50,
        "integration": 53,
        "contract": 27,
        "acceptance": 5,
    }


def test_database_backed_suites_have_canonical_integration_ownership() -> None:
    for old_path, new_path in RELOCATIONS.items():
        old = ROOT / old_path
        new = ROOT / new_path
        assert new.is_file(), new_path
        assert TOMBSTONE_MARKER not in new.read_text(encoding="utf-8")
        if old.exists():
            source = old.read_text(encoding="utf-8")
            assert TOMBSTONE_MARKER in source
            assert "def test_" not in source


def test_active_consumers_use_only_canonical_integration_paths() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in ACTIVE_CONSUMERS)
    for old_path, new_path in RELOCATIONS.items():
        assert old_path not in combined
        assert new_path in combined
