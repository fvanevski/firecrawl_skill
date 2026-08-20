from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = SCRIPTS.parent
INTEGRATION_TESTS = REPO_ROOT / "tests" / "integration"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(INTEGRATION_TESTS))

from research_store.fsearch_service import build_parser

from tests.integration.test_acquisition_authority import (
    _LEGACY_SURFACE_ALLOWLIST,
    _legacy_surface_inventory,
)


def test_runtime_legacy_surface_allowlist_is_empty():
    assert _LEGACY_SURFACE_ALLOWLIST == {}
    assert _legacy_surface_inventory(REPO_ROOT) == {}


def test_fsearch_parser_does_not_register_removed_replay_flags():
    option_strings = {
        option for action in build_parser()._actions for option in action.option_strings
    }
    assert "--reuse-search" not in option_strings
    assert "--scrape-ranks" not in option_strings


def test_aggregate_workflow_records_exact_head_and_required_contracts():
    workflow = (
        REPO_ROOT / ".github" / "workflows" / "authoritative-storage-gates.yml"
    ).read_text(encoding="utf-8")
    for required in (
        "github.event.pull_request.head.sha || github.sha",
        "git rev-parse HEAD",
        'test "$TESTED_SHA" = "$EXPECTED_SHA"',
        "actions/upload-artifact@v4",
        "concurrency:",
        "explicit-export-reproducibility",
        "authoritative-storage-gate-",
        "postgres:16-alpine",
        "qdrant/qdrant:v1.18.3-unprivileged",
        "valkey/valkey:8-alpine",
        "tests/integration/test_acquisition_authority.py",
        "tests/integration/test_authoritative_fsearch.py",
        "tests/integration/test_authoritative_fsearch_review.py",
        "tests/integration/test_direct_scrape_service.py",
        "tests/integration/test_authoritative_fscrape.py",
        "tests/unit/test_authoritative_fscrape_cli.py",
        "tests/integration/test_authoritative_smart_validation.py",
        "tests/acceptance/test_authoritative_live_validation_profiles.py",
        "tests/unit/test_database_native_inspection.py",
        "tests/integration/test_database_native_inspection_integration.py",
        "tests/integration/test_explicit_export_reproducibility.py",
        "tests/unit/test_index_runtime.py",
        "tests/integration/test_research_store_integration.py",
    ):
        assert required in workflow


def test_removed_flag_documentation_matches_parser_contract():
    documentation = (REPO_ROOT / "docs" / "authoritative-fsearch.md").read_text(
        encoding="utf-8"
    )
    option_strings = {
        option for action in build_parser()._actions for option in action.option_strings
    }

    assert "--dir" in option_strings
    assert "--reuse-search" not in option_strings
    assert "--scrape-ranks" not in option_strings
    assert "`--dir` remains a hidden compatibility tombstone" in documentation
    assert "`--reuse-search` and `--scrape-ranks` are not registered" in documentation
    assert "standard `unrecognized arguments` diagnostic" in documentation
