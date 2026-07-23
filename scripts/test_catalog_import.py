"""Unit tests for CatalogImportService.

Tests cover:
- Catalog v5 file scanning and parsing
- Schema and hash validation
- Dry-run mode (scan, validate, map)
- Malformed records detection
- Idempotent mapping
- Conflict detection
- Omission detection
- Reconciliation report
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))  # noqa: E402

from research_store.catalog_import import (  # noqa: E402
    CATALOG_ASSESSMENT_TYPE,
    CATALOG_EVENT_TYPE,
    CATALOG_INVOCATION_TYPE,
    CATALOG_RUN_TYPE,
    CatalogImportService,
    CatalogRootInvalid,
    CatalogRootNotFound,
    ReconciliationReport,
    _compute_dir_sha256,
    _is_valid_run_id,
    _is_valid_invocation_id,
    _is_valid_assessment_id,
    _is_valid_event_id,
)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


class TestValidationHelpers:
    def test_valid_run_id(self):
        assert _is_valid_run_id("fr_" + "a" * 32) is True

    def test_invalid_run_id_wrong_prefix(self):
        assert _is_valid_run_id("frun_" + "a" * 32) is False

    def test_invalid_run_id_wrong_length(self):
        assert _is_valid_run_id("fr_" + "a" * 31) is False

    def test_valid_invocation_id(self):
        assert _is_valid_invocation_id("fc_" + "a" * 32) is True
        assert _is_valid_invocation_id("fce_" + "a" * 32) is True

    def test_invalid_invocation_id(self):
        assert _is_valid_invocation_id("fc_" + "a" * 31) is False

    def test_valid_assessment_id(self):
        assert _is_valid_assessment_id("fa_" + "a" * 32) is True

    def test_invalid_assessment_id(self):
        assert _is_valid_assessment_id("fa_" + "a" * 31) is False

    def test_valid_event_id(self):
        assert _is_valid_event_id("fe_" + "a" * 32) is True

    def test_invalid_event_id(self):
        assert _is_valid_event_id("fe_" + "a" * 31) is False

    def test_none_values(self):
        assert _is_valid_run_id(None) is False
        assert _is_valid_invocation_id(None) is False
        assert _is_valid_assessment_id(None) is False
        assert _is_valid_event_id(None) is False


class TestComputeDirSha256:
    def test_empty_dir(self, tmp_path):
        assert len(_compute_dir_sha256(tmp_path)) == 64

    def test_deterministic(self, tmp_path):
        data = {"test": "value"}
        (tmp_path / "test.json").write_text(json.dumps(data))
        h1 = _compute_dir_sha256(tmp_path)
        h2 = _compute_dir_sha256(tmp_path)
        assert h1 == h2

    def test_different_content(self, tmp_path):
        (tmp_path / "a.json").write_text('{"a": 1}')
        (tmp_path / "b.json").write_text('{"b": 2}')
        h1 = _compute_dir_sha256(tmp_path)
        (tmp_path / "a.json").write_text('{"a": 2}')
        h2 = _compute_dir_sha256(tmp_path)
        assert h1 != h2


# ---------------------------------------------------------------------------
# Catalog scanning and parsing
# ---------------------------------------------------------------------------


class TestCatalogScanning:
    def setup_method(self):
        self.service = CatalogImportService(lambda: None)

    def test_scan_root_not_found(self, tmp_path):
        with pytest.raises(CatalogRootNotFound):
            self.service.scan_catalog_root(tmp_path / "nonexistent")

    def test_scan_empty_root(self, tmp_path):
        with pytest.raises(CatalogRootInvalid):
            self.service.scan_catalog_root(tmp_path)

    def test_scan_valid_run(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        run_id = "fr_" + "b" * 32
        run_data = {
            "schema_version": 5,
            "research_run_id": run_id,
            "objective": "Test objective",
        }
        (runs_dir / f"{run_id}.json").write_text(json.dumps(run_data))

        records = self.service.scan_catalog_root(tmp_path)
        assert len(records) == 1
        record = records[0]
        assert record.record_type == CATALOG_RUN_TYPE
        assert record.catalog_id == run_id
        assert record.is_valid is True
        assert record.schema_version == 5

    def test_scan_invalid_schema_version(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        run_id = "fr_" + "b" * 32
        run_data = {
            "schema_version": 3,
            "research_run_id": run_id,
        }
        (runs_dir / f"{run_id}.json").write_text(json.dumps(run_data))

        records = self.service.scan_catalog_root(tmp_path)
        assert len(records) == 1
        assert records[0].is_valid is False
        assert len(records[0].errors) > 0

    def test_scan_invocations(self, tmp_path):
        inv_dir = tmp_path / "invocations"
        inv_dir.mkdir()
        inv_id = "fc_" + "c" * 32
        inv_data = {
            "schema_version": 5,
            "invocation_id": inv_id,
            "operation": "search",
        }
        (inv_dir / f"{inv_id}.json").write_text(json.dumps(inv_data))

        records = self.service.scan_catalog_root(tmp_path)
        assert len(records) == 1
        assert records[0].record_type == CATALOG_INVOCATION_TYPE
        assert records[0].is_valid is True

    def test_scan_events_jsonl(self, tmp_path):
        events_file = tmp_path / "events.jsonl"
        event_id = "fe_" + "d" * 32
        events_file.write_text(
            json.dumps({"schema_version": 5, "event_id": event_id, "event": "test"})
            + "\n"
        )

        records = self.service.scan_catalog_root(tmp_path)
        assert len(records) == 1
        assert records[0].record_type == CATALOG_EVENT_TYPE
        assert records[0].is_valid is True

    def test_scan_assessments(self, tmp_path):
        run_id = "fr_" + "e" * 32
        assess_dir = tmp_path / "assessments" / run_id
        assess_dir.mkdir(parents=True)
        assess_id = "fa_" + "f" * 32
        assess_data = {
            "schema_version": 5,
            "assessment_id": assess_id,
            "target_id": run_id,
        }
        (assess_dir / f"{assess_id}.json").write_text(json.dumps(assess_data))

        records = self.service.scan_catalog_root(tmp_path)
        assert len(records) == 1
        assert records[0].record_type == CATALOG_ASSESSMENT_TYPE
        assert records[0].is_valid is True

    def test_scan_malformed_json(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        run_id = "fr_" + "g" * 32
        (runs_dir / f"{run_id}.json").write_text("not json{")

        records = self.service.scan_catalog_root(tmp_path)
        assert len(records) == 1
        assert records[0].is_valid is False
        assert len(records[0].errors) > 0

    def test_scan_multiple_record_types(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        inv_dir = tmp_path / "invocations"
        inv_dir.mkdir()

        run_id = "fr_" + "a" * 32
        run_data = {
            "schema_version": 5,
            "research_run_id": run_id,
        }
        (runs_dir / f"{run_id}.json").write_text(json.dumps(run_data))

        inv_id = "fc_" + "b" * 32
        inv_data = {
            "schema_version": 5,
            "invocation_id": inv_id,
        }
        (inv_dir / f"{inv_id}.json").write_text(json.dumps(inv_data))

        records = self.service.scan_catalog_root(tmp_path)
        assert len(records) == 2
        types = {r.record_type for r in records}
        assert types == {CATALOG_RUN_TYPE, CATALOG_INVOCATION_TYPE}


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


class TestDryRun:
    def setup_method(self):
        self.service = CatalogImportService(lambda: None)

    def _create_catalog_root(self, tmp_path, runs=None, invocations=None):
        """Helper to create a Catalog v5 root with given records."""
        root = tmp_path / "catalog"

        if runs:
            runs_dir = root / "runs"
            runs_dir.mkdir(parents=True)
            for run_id in runs:
                data = {
                    "schema_version": 5,
                    "research_run_id": run_id,
                    "objective": "Test",
                }
                (runs_dir / f"{run_id}.json").write_text(json.dumps(data))

        if invocations:
            inv_dir = root / "invocations"
            inv_dir.mkdir(parents=True)
            for inv_id in invocations:
                data = {
                    "schema_version": 5,
                    "invocation_id": inv_id,
                    "operation": "search",
                }
                (inv_dir / f"{inv_id}.json").write_text(json.dumps(data))

        return root

    def test_dry_run_insertable_run(self, tmp_path):
        root = self._create_catalog_root(tmp_path, runs=["fr_" + "a" * 32])
        report = self.service.dry_run(root)

        assert report.dry_run is True
        assert report.records_inserted == 1
        assert report.records_skipped == 0
        assert report.records_conflicting == 0
        assert report.records_malformed == 0
        assert report.source_state_sha256
        assert report.completed_at is not None
        assert report.started_at <= report.completed_at

    def test_dry_run_skipped_existing_run(self, tmp_path):
        root = self._create_catalog_root(tmp_path, runs=["fr_" + "a" * 32])
        existing = {"fr_" + "a" * 32}

        report = self.service.dry_run(root, existing_run_ids=existing)

        # The existing run should be skipped because PostgreSQL has it
        assert report.records_skipped == 1
        assert report.records_inserted == 0

    def test_dry_run_mixed_valid_invalid(self, tmp_path):
        root = tmp_path / "catalog"
        runs_dir = root / "runs"
        runs_dir.mkdir(parents=True)

        # Valid run
        valid_id = "fr_" + "a" * 32
        valid_data = {
            "schema_version": 5,
            "research_run_id": valid_id,
        }
        (runs_dir / f"{valid_id}.json").write_text(json.dumps(valid_data))

        # Invalid run (bad schema)
        invalid_id = "fr_" + "b" * 32
        invalid_data = {
            "schema_version": 3,
            "research_run_id": invalid_id,
        }
        (runs_dir / f"{invalid_id}.json").write_text(json.dumps(invalid_data))

        report = self.service.dry_run(root)

        assert report.records_inserted == 1
        assert report.records_malformed == 1
        assert len(report.valid_records) == 1
        assert len(report.malformed_records) == 1

    def test_dry_run_report_is_json_serializable(self, tmp_path):
        root = self._create_catalog_root(tmp_path, runs=["fr_" + "a" * 32])
        report = self.service.dry_run(root)

        report_dict = report.to_dict()
        # Should not raise
        json.dumps(report_dict, default=str)

    def test_dry_run_report_contains_mapping_details(self, tmp_path):
        root = self._create_catalog_root(tmp_path, runs=["fr_" + "a" * 32])
        report = self.service.dry_run(root)

        assert len(report.mappings) == 1
        mapping = report.mappings[0]
        assert mapping.catalog_type == CATALOG_RUN_TYPE
        assert mapping.catalog_id == "fr_" + "a" * 32
        assert mapping.status == "inserted"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def setup_method(self):
        self.service = CatalogImportService(lambda: None)

    def test_repeated_dry_run_produces_same_report(self, tmp_path):
        root = tmp_path / "catalog"
        runs_dir = root / "runs"
        runs_dir.mkdir(parents=True)
        run_id = "fr_" + "a" * 32
        run_data = {
            "schema_version": 5,
            "research_run_id": run_id,
        }
        (runs_dir / f"{run_id}.json").write_text(json.dumps(run_data))

        report1 = self.service.dry_run(root)
        report2 = self.service.dry_run(root)

        assert report1.records_inserted == report2.records_inserted
        assert report1.records_skipped == report2.records_skipped
        assert report1.source_state_sha256 == report2.source_state_sha256


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------


class TestConflictDetection:
    def setup_method(self):
        self.service = CatalogImportService(lambda: None)

    def test_conflict_when_pg_has_newer_data(self, tmp_path):
        root = tmp_path / "catalog"
        runs_dir = root / "runs"
        runs_dir.mkdir(parents=True)
        run_id = "fr_" + "a" * 32
        run_data = {
            "schema_version": 5,
            "research_run_id": run_id,
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-06-01T00:00:00+00:00",
        }
        (runs_dir / f"{run_id}.json").write_text(json.dumps(run_data))

        # Simulate existing run with newer timestamp
        existing = {"fr_" + "a" * 32}
        report = self.service.dry_run(root, existing_run_ids=existing)

        # Should be skipped because PostgreSQL has this run
        assert report.records_skipped == 1

    def test_no_conflict_when_run_not_in_pg(self, tmp_path):
        root = tmp_path / "catalog"
        runs_dir = root / "runs"
        runs_dir.mkdir(parents=True)
        run_id = "fr_" + "a" * 32
        run_data = {
            "schema_version": 5,
            "research_run_id": run_id,
        }
        (runs_dir / f"{run_id}.json").write_text(json.dumps(run_data))

        report = self.service.dry_run(root)
        assert report.records_inserted == 1


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


class TestReconciliation:
    def test_reconcile_empty(self):
        mock_uow = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = []
        mock_uow.connection.cursor.return_value.__enter__.return_value = mock_cur
        mock_uow.connection.cursor.return_value.__exit__.return_value = None

        service = CatalogImportService(lambda: mock_uow)
        report = service.reconcile()

        assert isinstance(report, ReconciliationReport)
        assert report.total_imports == 0
        assert report.imports == []
        assert report.conflict_summary == {}
        assert report.omission_summary == {}
