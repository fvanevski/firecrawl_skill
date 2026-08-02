"""Collection shim for legacy bridge tests and the authoritative fscrape launcher."""

import runpy
from pathlib import Path

_CASES = runpy.run_path(
    str(Path(__file__).with_name("test_fsearch_wrapper_e2e.py.inc"))
)
globals().update(
    {
        name: value
        for name, value in _CASES.items()
        if not name.startswith("__") and name != "TestWrapperContracts"
    }
)


class TestWrapperContracts(_CASES["TestWrapperContracts"]):
    """Verify both migrated authoritative launchers."""

    def test_fscrape_manifest_contains_results(self, tmp_path, monkeypatch):
        """fscrape is a thin authoritative Python entrypoint, not a manifest writer."""
        fscrape_path = Path(__file__).resolve().parent / "fscrape"
        assert fscrape_path.is_file()

        content = fscrape_path.read_text()
        assert content.startswith("#!/usr/bin/env bash")
        assert "research-env" in content
        assert "FIRECRAWL_RESEARCH_PYTHON" in content
        assert "-m research_store.fscrape_cli" in content
        for removed in (
            '"results"',
            '"_meta.json"',
            "firecrawl scrape",
            "mkdir ",
            " -o ",
            "python3 - <<",
        ):
            assert removed not in content
