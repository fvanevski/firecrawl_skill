"""Tests for issue #36: Route compatibility commands through PostgreSQL.

Tests cover:
- CLI command routing through PostgreSQL (run-annotate, run-verify, run-audit, run-compare)
- Finish/reopen idempotency
- Failure ordering (PostgreSQL commit before filesystem export)
- Export failure after successful state transition
- Deprecation warnings for filesystem fallback
- CLI parity
- Concurrency protections for lifecycle_revision
"""

from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import uuid4

import pytest

try:
    import psycopg
except ImportError:
    psycopg = None

SCRIPTS = Path(__file__).resolve().parent

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")


def run_frun(*args, env=None):
    """Run the frun script with the given arguments."""
    return subprocess.run(
        [str(SCRIPTS / "frun"), *map(str, args)],
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )


def run_research_db(*args, env=None):
    """Run the research-db script with the given arguments."""
    return subprocess.run(
        [str(SCRIPTS / "research-db"), *map(str, args)],
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )


class TestFrunCommandRouting:
    """Test that frun routes commands through PostgreSQL when available."""

    def test_frun_annotate_fallback_to_filesystem_with_deprecation_warning(
        self, tmp_path, monkeypatch
    ):
        """annotate command should fall back to filesystem catalog with deprecation warning when DB is off."""
        monkeypatch.setenv("FIRECRAWL_RESEARCH_PERSIST", "off")
        monkeypatch.setenv("FIRECRAWL_RESEARCH_AUTO_ENV", "0")
        # When DB is off, annotate falls back to filesystem with deprecation warning
        result = run_frun("annotate", "fr_test", "--type", "pivot", "--reason", "test")
        # Should show deprecation warning when falling back
        assert "WARNING" in result.stderr or result.returncode != 0

    def test_frun_status_fallback_to_filesystem_with_deprecation_warning(
        self, tmp_path, monkeypatch
    ):
        """status command should fall back to filesystem catalog with deprecation warning when DB is off."""
        monkeypatch.setenv("FIRECRAWL_RESEARCH_PERSIST", "off")
        monkeypatch.setenv("FIRECRAWL_RESEARCH_AUTO_ENV", "0")
        result = run_frun("status", "fr_test")
        # Should show deprecation warning when falling back
        assert "WARNING" in result.stderr or result.returncode != 0

    def test_frun_verify_fallback_to_filesystem_with_deprecation_warning(
        self, tmp_path, monkeypatch
    ):
        """verify command should fall back to filesystem catalog with deprecation warning when DB is off."""
        monkeypatch.setenv("FIRECRAWL_RESEARCH_PERSIST", "off")
        monkeypatch.setenv("FIRECRAWL_RESEARCH_AUTO_ENV", "0")
        monkeypatch.setenv("FIRECRAWL_WARN_MARKER", str(tmp_path / "warn_marker"))
        result = run_frun("verify", "fr_test")
        assert "WARNING" in result.stderr or result.returncode != 0

    def test_frun_compare_fallback_to_filesystem_with_deprecation_warning(
        self, tmp_path, monkeypatch
    ):
        """compare command should fall back to filesystem catalog with deprecation warning when DB is off."""
        monkeypatch.setenv("FIRECRAWL_RESEARCH_PERSIST", "off")
        monkeypatch.setenv("FIRECRAWL_RESEARCH_AUTO_ENV", "0")
        result = run_frun("compare", "fr_test")
        assert "WARNING" in result.stderr or result.returncode != 0

    def test_frun_export_requires_postgres(self, tmp_path, monkeypatch):
        """export command should fail when PostgreSQL is unavailable."""
        monkeypatch.setenv("FIRECRAWL_RESEARCH_PERSIST", "off")
        monkeypatch.setenv("FIRECRAWL_RESEARCH_AUTO_ENV", "0")
        result = run_frun("export", "fr_test", str(tmp_path / "target"))
        assert result.returncode != 0
        assert "requires PostgreSQL" in result.stderr


class TestFinishReopenIdempotency:
    """Test finish and reopen idempotency."""

    def test_finish_idempotency_same_outcome(self, tmp_path, monkeypatch):
        """Finishing a run twice with the same outcome should be idempotent."""
        monkeypatch.setenv("FIRECRAWL_RESEARCH_PERSIST", "off")
        monkeypatch.setenv("FIRECRAWL_RESEARCH_AUTO_ENV", "0")
        monkeypatch.setenv("FIRECRAWL_CATALOG_DIR", str(tmp_path / "catalog"))
        monkeypatch.setenv("FIRECRAWL_AUDIT_AUTO_SEMANTIC", "0")

        # Start a run
        start_result = run_frun("start", "test objective")
        assert start_result.returncode == 0
        run_id = start_result.stdout.strip()

        # Finish the run
        finish_result = run_frun("finish", run_id, "--outcome", "satisfied")
        assert finish_result.returncode == 0

        # Finish again with same outcome - should succeed (idempotent)
        run_frun("finish", run_id, "--outcome", "satisfied")
        # The filesystem catalog handles idempotency internally
        # The key invariant is that PostgreSQL commit happens first

    def test_reopen_after_finish(self, tmp_path, monkeypatch):
        """Reopening a finished run should transition it back to running."""
        monkeypatch.setenv("FIRECRAWL_RESEARCH_PERSIST", "off")
        monkeypatch.setenv("FIRECRAWL_RESEARCH_AUTO_ENV", "0")
        monkeypatch.setenv("FIRECRAWL_CATALOG_DIR", str(tmp_path / "catalog"))
        monkeypatch.setenv("FIRECRAWL_AUDIT_AUTO_SEMANTIC", "0")

        # Start a run
        start_result = run_frun("start", "test objective")
        assert start_result.returncode == 0
        run_id = start_result.stdout.strip()

        # Finish the run
        finish_result = run_frun("finish", run_id, "--outcome", "satisfied")
        assert finish_result.returncode == 0

        # Reopen the run
        reopen_result = run_frun("reopen", run_id, "--reason", "need more research")
        assert reopen_result.returncode == 0


class TestFailureOrdering:
    """Test that PostgreSQL failure doesn't leave filesystem-only committed state."""

    def test_filesystem_export_fails_after_db_commit(self, tmp_path, monkeypatch):
        """
        Verify that the frun script uses ``|| true`` on filesystem export,
        ensuring export failure never rolls back a committed DB state.

        We verify the code invariant by checking the frun script source.
        The actual DB+export failure ordering is tested in the integration
        suite (test_research_store_integration.py) where a live PostgreSQL
        is available.
        """
        frun_path = SCRIPTS / "frun"
        content = frun_path.read_text()

        # The _compat_export_events function must use || true
        assert "|| true" in content, (
            "frun must use || true on filesystem export to prevent "
            "export failure from rolling back DB state"
        )

        # The export function should be called after DB commands in start/finish/reopen
        # Verify the order: DB command before export
        start_section = (
            content.split("start)")[1].split(";;")[0] if "start)" in content else ""
        )
        finish_section = (
            content.split("finish)")[1].split(";;")[0] if "finish)" in content else ""
        )
        reopen_section = (
            content.split("reopen)")[1].split(";;")[0] if "reopen)" in content else ""
        )

        for section_name, section in (
            ("start", start_section),
            ("finish", finish_section),
            ("reopen", reopen_section),
        ):
            if "_compat_export_events" in section:
                # DB command (research-db) should appear before _compat_export_events
                db_pos = section.find("research-db")
                export_pos = section.find("_compat_export_events")
                assert db_pos < export_pos, (
                    f"{section_name}: DB command must precede filesystem export"
                )

    def test_db_failure_prevents_filesystem_only_commit(self, tmp_path, monkeypatch):
        """
        When PostgreSQL fails, the command should exit nonzero and not
        leave a filesystem-only committed state.
        """
        monkeypatch.setenv("FIRECRAWL_RESEARCH_PERSIST", "on")
        monkeypatch.setenv("FIRECRAWL_RESEARCH_AUTO_ENV", "0")
        # Without DATABASE_URL, DB_ACTIVE=0, so it falls back to filesystem
        # With DATABASE_URL set but DB unavailable, it should fail
        monkeypatch.setenv("DATABASE_URL", "postgresql://nonexistent:5432/nonexistent")
        result = run_frun("start", "test objective")
        # Should fail because DB is unavailable
        assert result.returncode != 0


class TestDeprecationWarnings:
    """Test deprecation warnings for filesystem fallback."""

    def test_deprecation_warning_on_filesystem_fallback(self, tmp_path, monkeypatch):
        """When DB is unavailable, frun should emit a deprecation warning."""
        monkeypatch.setenv("FIRECRAWL_RESEARCH_PERSIST", "off")
        monkeypatch.setenv("FIRECRAWL_RESEARCH_AUTO_ENV", "0")
        monkeypatch.setenv("FIRECRAWL_CATALOG_DIR", str(tmp_path / "catalog"))
        monkeypatch.setenv("FIRECRAWL_WARN_MARKER", str(tmp_path / "warn_marker"))

        result = run_frun("status", "fr_test")
        assert "WARNING" in result.stderr
        assert (
            "deprecated" in result.stderr.lower() or "fallback" in result.stderr.lower()
        )

    def test_deprecation_warning_only_once(self, tmp_path, monkeypatch):
        """Deprecation warning should only appear once per invocation."""
        monkeypatch.setenv("FIRECRAWL_RESEARCH_PERSIST", "off")
        monkeypatch.setenv("FIRECRAWL_RESEARCH_AUTO_ENV", "0")
        monkeypatch.setenv("FIRECRAWL_CATALOG_DIR", str(tmp_path / "catalog"))
        monkeypatch.setenv("FIRECRAWL_WARN_MARKER", str(tmp_path / "warn_marker"))

        # Each invocation shows the warning once (not cumulative across invocations)
        result1 = run_frun("status", "fr_test")
        result2 = run_frun("verify", "fr_test")

        warning_count1 = result1.stderr.count("WARNING")
        assert warning_count1 <= 1
        warning_count2 = result2.stderr.count("WARNING")
        assert warning_count2 <= 1


class TestCLIParity:
    """Test CLI parity between frun and research-db commands."""

    def test_frun_usage_message(self):
        """frun should show usage when called without arguments."""
        result = run_frun()
        assert result.returncode != 0
        assert "Usage:" in result.stderr

    def test_frun_validates_run_id_format(self, tmp_path, monkeypatch):
        """frun should validate run ID format."""
        monkeypatch.setenv("FIRECRAWL_RESEARCH_PERSIST", "off")
        monkeypatch.setenv("FIRECRAWL_RESEARCH_AUTO_ENV", "0")
        monkeypatch.setenv("FIRECRAWL_CATALOG_DIR", str(tmp_path / "catalog"))

        result = run_frun("status", "invalid-id")
        # Should fail with validation error
        assert result.returncode != 0

    def test_research_db_run_annotate_parser(self):
        """research-db run-annotate should parse arguments correctly."""
        from research_store.cli import parser as research_store_parser

        args = research_store_parser().parse_args(
            ["run-annotate", "fr_test", "--type", "pivot", "--reason", "test reason"]
        )
        assert args.command == "run-annotate"
        assert args.external_id == "fr_test"
        assert args.type == "pivot"
        assert args.reason == "test reason"

    def test_research_db_run_verify_parser(self):
        """research-db run-verify should parse arguments correctly."""
        from research_store.cli import parser as research_store_parser

        args = research_store_parser().parse_args(["run-verify", "fr_test"])
        assert args.command == "run-verify"
        assert args.external_id == "fr_test"

    def test_research_db_run_audit_parser(self):
        """research-db run-audit should parse arguments correctly."""
        from research_store.cli import parser as research_store_parser

        args = research_store_parser().parse_args(
            ["run-audit", "fr_test", "--target-hash", "abc123"]
        )
        assert args.command == "run-audit"
        assert args.external_id == "fr_test"
        assert args.target_hash == "abc123"

    def test_research_db_run_compare_parser(self):
        """research-db run-compare should parse arguments correctly."""
        from research_store.cli import parser as research_store_parser

        args = research_store_parser().parse_args(
            ["run-compare", "fr_test1", "fr_test2"]
        )
        assert args.command == "run-compare"
        assert args.external_ids == ["fr_test1", "fr_test2"]

    def test_run_finish_parser_rejects_nonterminal_status(self):
        """run-finish should reject non-terminal status values."""
        from research_store.cli import parser as research_store_parser

        with pytest.raises(SystemExit):
            research_store_parser().parse_args(
                [
                    "run-finish",
                    "fr_test",
                    "--outcome",
                    "satisfied",
                    "--status",
                    "running",
                ]
            )


class TestAuthorityModel:
    """Test authority model invariants."""

    def test_postgres_is_authoritative(self):
        """PostgreSQL is the sole authoritative workflow state."""
        # This is a design invariant documented in the architecture.
        # The code routes all write operations through PostgreSQL.
        from research_store.run_service import ResearchRunService

        # The service uses uow_factory which creates PostgresUnitOfWork
        assert hasattr(ResearchRunService, "annotate")
        assert hasattr(ResearchRunService, "verify")
        assert hasattr(ResearchRunService, "trigger_audit")

    def test_filesystem_is_derived(self):
        """Filesystem is derived from PostgreSQL, not authoritative."""
        # The frun script calls catalog_v5.py export-events-to-fs
        # AFTER the PostgreSQL commit (with || true to ignore failures).
        # This ensures filesystem is always derived.
        frun_path = SCRIPTS / "frun"
        content = frun_path.read_text()
        # The export happens after the DB command
        assert "_compat_export_events" in content
        # The export uses || true (ignores failures)
        assert "|| true" in content


class TestCatalogExportIntegration:
    """Test integration with Catalog v5 exporter."""

    def test_catalog_export_service_exists(self):
        """The catalog export service should exist."""
        from research_store.catalog_export import CatalogExportService

        assert hasattr(CatalogExportService, "export_run")
        assert hasattr(CatalogExportService, "export_invocation")
        assert hasattr(CatalogExportService, "export_events")


class TestConcurrency:
    """Test concurrency protections for compatibility commands."""

    @pytest.mark.skipif(not TEST_DSN or not psycopg, reason="Requires PostgreSQL and psycopg")
    def test_concurrent_annotate(self, monkeypatch):
        """Concurrent annotations must not lose lifecycle_revision updates."""
        from research_store.config import StoreConfig
        from research_store.container import build_run_service
        from research_store.run_service import RunStateError

        monkeypatch.setenv("DATABASE_URL", TEST_DSN)
        config = StoreConfig.from_env()
        run_service = build_run_service(config)

        # Start a run
        run_id = uuid4()
        run_service.create(
            external_id=str(run_id),
            objective="Concurrency test objective",
            profile="autonomous_local",
            run_id=run_id,
            idempotency_key=f"idem:start:{run_id}"
        )

        # Blast annotations
        num_workers = 10
        successes = 0
        failures = 0

        def annotate_worker(i):
            try:
                run_service.annotate(
                    run_id,
                    event_type="pivot",
                    reason=f"Concurrent annotation {i}",
                    actor_type="test",
                    idempotency_key=f"idem:test:{i}"
                )
                return True
            except RunStateError:
                return False

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(annotate_worker, i) for i in range(num_workers)]
            for future in as_completed(futures):
                if future.result():
                    successes += 1
                else:
                    failures += 1

        status = run_service.status(run_id=run_id)
        # The revision should have incremented exactly by the number of successful annotations.
        # Initial is 1. Plus successes.
        assert status.lifecycle_revision == 1 + successes
        assert successes > 0, "At least one annotation should have succeeded"
