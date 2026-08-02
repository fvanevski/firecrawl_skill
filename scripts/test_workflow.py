"""Collection shim for the workflow tests with the authoritative fscrape contract."""

import runpy
from pathlib import Path

_CASES = runpy.run_path(str(Path(__file__).with_name("test_workflow_cases.py.inc")))
globals().update(
    {name: value for name, value in _CASES.items() if not name.startswith("__")}
)


def test_fscrape_preserves_multiple_urls_and_schema(fake_cli):
    """The removed scratch export fails before provider invocation."""
    env, tmp_path = fake_cli
    output = tmp_path / "batch with spaces"

    result = _CASES["run_script"](
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
