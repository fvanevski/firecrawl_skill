"""Workflow tests with the authoritative fscrape compatibility contract."""

import json
from pathlib import Path

from fixtures.workflow_test_cases import *
from fixtures.workflow_test_cases import run_script


def test_fscrape_rejects_undocumented_format(fake_cli):
    env, _ = fake_cli

    result = run_script(
        "fscrape",
        "https://example.com",
        "--format",
        "text",
        env=env,
    )

    assert result.returncode == 2
    assert "invalid choice" in result.stderr
    assert not Path(env["FAKE_FIRECRAWL_LOG"]).exists()


def test_fscrape_preserves_multiple_urls_and_schema(fake_cli):
    """The removed storage export fails before provider invocation."""
    env, tmp_path = fake_cli
    output = tmp_path / "batch with spaces"

    result = run_script(
        "fscrape",
        "https://example.com/a,b",
        "https://example.com/two",
        "--schema",
        '{"type":"object","properties":{"name":{"type":"string"}}}',
        "--output-dir",
        output,
        env=env,
    )

    assert result.returncode == 2
    assert "--output-dir was removed" in result.stderr
    assert "database-native export" in result.stderr
    assert not output.exists()
    assert not Path(env["FAKE_FIRECRAWL_LOG"]).exists()


def test_smart_search_writes_diagnostic_dry_run_artifacts(fake_cli):
    """RC-7 dry-run replaces diagnostic files with structured stdout only."""
    env, tmp_path = fake_cli
    temporary = tmp_path / "smart tmp"
    env["TMPDIR"] = str(temporary)
    env.pop("GOOGLE_API_KEY", None)

    result = run_script(
        "fsearch_smart",
        "portable wrapper",
        "--dry-run",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "authoritative-smart-search-plan-v1"
    assert payload["mode"] == "dry_run"
    assert payload["planner"] == "deterministic_preview"
    assert payload["budget_snapshot"]["policy_version"] == "budget-policy-v1"
    assert payload["queries"][0]["query"] == "portable wrapper"
    assert not temporary.exists() or list(temporary.rglob("*")) == []
    assert not Path(env["FAKE_FIRECRAWL_LOG"]).exists()
