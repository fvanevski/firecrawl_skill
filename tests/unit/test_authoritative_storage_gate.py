from __future__ import annotations

import sys
import tomllib
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = SCRIPTS.parent
INTEGRATION_TESTS = REPO_ROOT / "tests" / "integration"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(INTEGRATION_TESTS))

from firecrawl_skill.research_store.fsearch_service import build_parser
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


def test_central_storage_profile_records_required_service_contracts():
    authority = tomllib.loads(
        (REPO_ROOT / "ci" / "test-profiles.toml").read_text(encoding="utf-8")
    )
    storage = authority["profiles"]["storage"]
    assert authority["python_version"] == "3.12"
    assert set(storage["services"]) == {"postgres", "qdrant", "valkey"}
    for token in ("postgres", "storage", "qdrant", "index_runtime", "index_census"):
        assert token in storage["ownership_tokens"]
    assert "tests/integration/test_audit_persistence.py" in storage["selectors"]

    runner = (REPO_ROOT / "scripts" / "run_ci_profile.py").read_text(encoding="utf-8")
    assert "scripts/disposable-test-services" in runner
    assert '"valkey/valkey:8-alpine"' in runner


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
