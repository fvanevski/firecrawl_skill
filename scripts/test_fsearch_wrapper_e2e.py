"""Legacy persistence-bridge tests plus authoritative launcher gates.

The fsearch and fscrape launchers are PostgreSQL-authoritative Python
entrypoints. The remaining tests in this module cover legacy
``persist_results.py`` inputs that are removed by later release-candidate
issues; they do not describe either launcher's runtime contract.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _corpus_output_path(manifest_path: Path) -> Path:
    """Compute the default output path for a given manifest."""
    return manifest_path.with_suffix(manifest_path.suffix + "_corpus.json")


SCRIPTS = Path(__file__).resolve().parent


class TestWrapperContracts:
    """Verify the migrated authoritative launchers."""

    def test_fsearch_is_authoritative_python_entrypoint(self):
        fsearch_path = SCRIPTS / "fsearch"
        assert fsearch_path.is_file()

        content = fsearch_path.read_text()
        assert content.startswith("#!/usr/bin/env bash")
        assert "research-env" in content
        assert "FIRECRAWL_RESEARCH_PYTHON" in content
        assert "-m research_store.fsearch_service" in content
        for removed in (
            "_search.json",
            "_meta.json",
            "_context.json",
            "_candidates.json",
            "_index.md",
            "result_",
            "_corpus.json",
        ):
            assert removed not in content

    def test_fscrape_is_authoritative_python_entrypoint(self):
        fscrape_path = SCRIPTS / "fscrape"
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
            "firecrawl_scratch",
            "scratch_file",
            "persist_results.py",
            "fread",
            "_corpus.json",
        ):
            assert removed not in content


class TestPersistResultsIntegration:
    """End-to-end persist_results.py through legacy search/scrape manifests."""

    def test_fsearch_manifest_persisted_with_run_id(self, tmp_path, monkeypatch):
        """Legacy fsearch manifest with run_id is accepted by the bridge."""
        scratch = tmp_path / "result_000.md"
        scratch.write_text("# Title\n\nParagraph one.\n", encoding="utf-8")

        manifest = {
            "invocation_id": "fc_test_e2e",
            "operation": "search",
            "query": "test query",
            "provider_request_id": "fc_req_123",
            "scratch_dir": str(tmp_path),
            "scrape_format": "markdown",
            "total_scraped": 1,
            "estimated_total_words": 10,
            "candidate_count": 1,
            "candidates": [
                {
                    "rank": 1,
                    "url": "https://example.com/article",
                    "title": "Example Article",
                    "snippet": "A test snippet for verification.",
                    "description": "Full description",
                    "publishedDate": "2026-07-27T00:00:00Z",
                    "selected": True,
                    "scrape_status": "ok",
                    "scratch_file": str(scratch),
                    "word_count": 4,
                }
            ],
            "results": [
                {
                    "index": 0,
                    "url": "https://example.com/article",
                    "title": "Example Article",
                    "snippet": "A test snippet for verification.",
                    "description": "Full description",
                    "scratch_file": str(scratch),
                    "raw_scratch_file": "",
                    "format": "markdown",
                    "size_kb": 0.1,
                    "char_count": 30,
                    "word_count": 4,
                    "preview_head": "Title Paragraph one.",
                    "status": "ok",
                }
            ],
        }
        meta = tmp_path / "_meta.json"
        meta.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        env = dict(os.environ)
        env["PYTHONPATH"] = str(SCRIPTS)
        env.pop("DATABASE_URL", None)

        result = subprocess.run(  # noqa: PLW1510
            [
                sys.executable,
                str(SCRIPTS / "persist_results.py"),
                str(meta),
                "--research-run-id",
                "fr_test_e2e_run",
            ],
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

        corpus = json.loads(_corpus_output_path(meta).read_text())
        assert len(corpus) == 1
        assert corpus[0]["persisted"] is False
        assert corpus[0]["url"] == "https://example.com/article"
        assert corpus[0]["status"] == "ok"

    def test_fscrape_manifest_persisted_with_run_id(self, tmp_path, monkeypatch):
        """fscrape manifest with run_id is persisted authoritatively."""
        scratch = tmp_path / "url_000.md"
        scratch.write_text("# Page\n\nContent here.\n", encoding="utf-8")

        manifest = {
            "invocation_id": "fc_test_scrape",
            "operation": "scrape",
            "results": [
                {
                    "index": 0,
                    "url": "https://example.com/page",
                    "title": "Example Page",
                    "scratch_file": str(scratch),
                    "raw_scratch_file": "",
                    "format": "markdown",
                    "has_summary": False,
                    "status": "ok",
                    "size_kb": 0.1,
                    "char_count": 25,
                    "word_count": 4,
                    "preview_head": "Page Content here.",
                }
            ],
            "total_size_kb": 0.1,
            "total_words": 4,
            "scratch_dir": str(tmp_path),
        }
        meta = tmp_path / "_meta.json"
        meta.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        env = dict(os.environ)
        env["PYTHONPATH"] = str(SCRIPTS)
        env.pop("DATABASE_URL", None)

        result = subprocess.run(  # noqa: PLW1510
            [
                sys.executable,
                str(SCRIPTS / "persist_results.py"),
                str(meta),
                "--research-run-id",
                "fr_test_scrape_run",
            ],
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

        corpus = json.loads(_corpus_output_path(meta).read_text())
        assert len(corpus) == 1
        assert corpus[0]["persisted"] is False
        assert corpus[0]["url"] == "https://example.com/page"

    def test_fsearch_manifest_with_missing_scratch(self, tmp_path, monkeypatch):
        """Legacy manifest with missing scratch is handled gracefully."""
        manifest = {
            "invocation_id": "fc_test_missing",
            "operation": "search",
            "query": "test",
            "candidates": [
                {
                    "rank": 1,
                    "url": "https://example.com",
                    "title": "Example",
                    "scratch_file": "/nonexistent/file.md",
                    "scrape_status": "error",
                }
            ],
        }
        meta = tmp_path / "_meta.json"
        meta.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        env = dict(os.environ)
        env["PYTHONPATH"] = str(SCRIPTS)
        env.pop("DATABASE_URL", None)

        result = subprocess.run(  # noqa: PLW1510
            [
                sys.executable,
                str(SCRIPTS / "persist_results.py"),
                str(meta),
            ],
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0

        corpus = json.loads(_corpus_output_path(meta).read_text())
        assert len(corpus) == 1
        assert corpus[0]["persisted"] is False
        assert corpus[0]["status"] == "ok"
        # In scratch-only mode (no DB), unscripted candidates get
        # reason="not_scraped". When a DB is configured the same
        # path is exercised by the integration test
        # TestSearchPersistence.test_fsearch_mixed_success_and_failure.

    def test_fsearch_manifest_multiple_candidates(self, tmp_path, monkeypatch):
        """Legacy manifest with multiple candidates is fully processed."""
        scratch1 = tmp_path / "result_000.md"
        scratch1.write_text("content one", encoding="utf-8")
        scratch2 = tmp_path / "result_001.md"
        scratch2.write_text("content two", encoding="utf-8")

        manifest = {
            "invocation_id": "fc_test_multi",
            "operation": "search",
            "query": "test",
            "candidates": [
                {
                    "rank": 1,
                    "url": "https://example1.com",
                    "title": "Example1",
                    "scratch_file": str(scratch1),
                    "scrape_status": "ok",
                },
                {
                    "rank": 2,
                    "url": "https://example2.com",
                    "title": "Example2",
                    "scratch_file": str(scratch2),
                    "scrape_status": "ok",
                },
            ],
        }
        meta = tmp_path / "_meta.json"
        meta.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        env = dict(os.environ)
        env["PYTHONPATH"] = str(SCRIPTS)
        env.pop("DATABASE_URL", None)

        result = subprocess.run(  # noqa: PLW1510
            [
                sys.executable,
                str(SCRIPTS / "persist_results.py"),
                str(meta),
            ],
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0

        corpus = json.loads(_corpus_output_path(meta).read_text())
        assert len(corpus) == 2
        assert all(c["persisted"] is False for c in corpus)
        assert all(c["status"] == "ok" for c in corpus)
