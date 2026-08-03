from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from research_store.fsearch_service import build_parser
from test_acquisition_authority import (
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
        "scripts/test_acquisition_authority.py",
        "scripts/test_authoritative_fsearch.py",
        "scripts/test_authoritative_fsearch_review.py",
        "scripts/test_direct_scrape_service.py",
        "scripts/test_authoritative_fscrape.py",
        "scripts/test_authoritative_fscrape_cli.py",
        "scripts/test_authoritative_smart_validation.py",
        "scripts/test_authoritative_live_validation_profiles.py",
        "scripts/test_database_native_inspection.py",
        "scripts/test_database_native_inspection_integration.py",
        "scripts/test_explicit_export_reproducibility.py",
        "scripts/test_index_runtime.py",
        "scripts/test_research_store_integration.py",
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
