"""Workflow tests with the authoritative fscrape compatibility contract."""

from pathlib import Path

from fixtures.workflow_test_cases import fake_cli as _workflow_fake_cli
from fixtures.workflow_test_cases import run_script

fake_cli = _workflow_fake_cli


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


def test_smart_compatibility_name_has_no_diagnostic_dry_run_surface() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "scripts" / "fsearch_smart"
    ).read_text(encoding="utf-8")
    assert "--dry-run" not in source
    assert "deterministic_preview" not in source
    assert 'with_name("fresearch")' in source
    assert "os.execv" in source
