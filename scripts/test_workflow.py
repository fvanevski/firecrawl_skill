"""Collection shim for the workflow tests with the authoritative fscrape contract."""

from pathlib import Path

import workflow_test_cases as cases
from workflow_test_cases import *  # noqa: F403


def test_fscrape_preserves_multiple_urls_and_schema(fake_cli):
    """The removed scratch export fails before provider invocation."""
    env, tmp_path = fake_cli
    output = tmp_path / "batch with spaces"

    result = cases.run_script(
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
