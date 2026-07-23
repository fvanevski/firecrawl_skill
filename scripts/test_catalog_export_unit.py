from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from uuid import UUID

import pytest

SCRIPTS = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(SCRIPTS))

import catalog_v5
from research_store.catalog_export import (
    SCHEMA_VERSION,
    _atomic_write_json,
    _catalog_id,
    _map_event,
    CatalogExportService,
)


def test_checked_in_golden_layout_and_real_readers(tmp_path, monkeypatch):
    golden = json.loads(
        (SCRIPTS / "testdata" / "catalog_v5_export_golden.json").read_text()
    )
    root = tmp_path / "catalog"
    run_path = root / golden["run_path"]
    invocation_path = root / golden["invocation_path"]
    assessment_path = root / golden["assessment_path"]
    run_path.parent.mkdir(parents=True)
    invocation_path.parent.mkdir(parents=True)
    assessment_path.parent.mkdir(parents=True)
    run_path.write_text(json.dumps(golden["run"]))
    invocation_path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "invocation_id": invocation_path.stem,
        "research_run_id": golden["run"]["research_run_id"],
        "operation": "search",
        "input": {"query": "golden"},
        "execution": {"status": "succeeded"},
        "events": [], "results": [], "artifacts": [], "assessment_refs": [],
    }))
    assessment_path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "assessment_id": assessment_path.stem,
        "target_id": golden["run"]["research_run_id"],
        "target_hash": "0" * 64,
        "evaluator_version": "catalog-v5.0",
        "stages": {}, "stage_errors": [], "calls": [], "status": "completed",
    }))
    (root / "events.jsonl").write_text(json.dumps(golden["event"]) + "\n")
    monkeypatch.setenv("FIRECRAWL_CATALOG_DIR", str(root))

    assert catalog_v5.read_run(golden["run"]["research_run_id"]) == golden["run"]
    assert catalog_v5.read_record(invocation_path.stem)["invocation_id"] == invocation_path.stem
    assert catalog_v5.read_path(catalog_v5.assessment_path(golden["run"]["research_run_id"], assessment_path.stem))["calls"] == []
    assert catalog_v5._events_for_run(golden["run"]["research_run_id"]) == [golden["event"]]
    assert catalog_v5.build_audit_packet(golden["run"]["research_run_id"])["target_id"] == golden["run"]["research_run_id"]


def test_catalog_ids_are_deterministic_and_reader_dispatchable():
    run_id = UUID("00000000-0000-0000-0000-000000000001")
    invocation_id = UUID("00000000-0000-0000-0000-000000000002")
    assert _catalog_id("fr_", run_id) == "fr_" + run_id.hex
    assert _catalog_id("fc_", invocation_id) == "fc_" + invocation_id.hex


def test_event_mapping_retains_run_identity_and_sequence():
    run_id = "fr_" + "1" * 32
    event = _map_event({
        "id": UUID("00000000-0000-0000-0000-000000000004"),
        "event_type": "run_started", "created_at": "2026-01-01T00:00:00+00:00",
        "invocation_id": None, "payload": {}, "run_revision": 1,
        "sequence_number": 7,
    }, run_id, {})
    assert event["research_run_id"] == run_id
    assert event["sequence_number"] == 7


def test_unique_atomic_temporaries_survive_concurrent_writers(tmp_path):
    target = tmp_path / "record.json"
    payloads = [{"writer": value, "body": "x" * 10000} for value in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda payload: _atomic_write_json(target, payload), payloads))
    assert json.loads(target.read_text()) in payloads
    assert not list(tmp_path.glob("*.tmp"))


def test_postgresql_failure_prevents_filesystem_publication(tmp_path):
    def unavailable_uow():
        raise RuntimeError("database unavailable")

    target = tmp_path / "must-not-exist"
    with pytest.raises(RuntimeError, match="database unavailable"):
        CatalogExportService(unavailable_uow).export_run(
            UUID("00000000-0000-0000-0000-000000000001"), target
        )
    assert not target.exists()
