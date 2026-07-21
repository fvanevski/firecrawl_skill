from __future__ import annotations

# ruff: noqa: E402

from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.config import StoreConfig
from research_store.container import (

    build_acquisition_service,
    build_compatibility_export_service,
    build_run_service,
)
from research_store.postgres import connect, migrate, require_disposable_database_reset


TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def prepared_database():
    """Ensure database schema is up-to-date for integration tests."""
    if not TEST_DSN:
        return
    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    assert migrate(TEST_DSN) >= 11


class MockSearchAdapter:
    def search(self, query_text: str, **kwargs):
        from research_store.domain import SearchAdapterResult, utcnow

        payload = json.dumps({
            "success": True,
            "data": [
                {
                    "url": "https://export-test.com/item1",
                    "title": "Export Item One",
                    "snippet": "Snippet for export item one",
                },
                {
                    "url": "https://export-test.org/item2",
                    "title": "Export Item Two",
                    "snippet": "Snippet for export item two",
                },
            ],
        }).encode("utf-8")
        return SearchAdapterResult(
            raw_payload=payload,
            http_status=200,
            provider_request_id=f"req-{uuid4()}",
            transport_error=None,
            requested_at=utcnow(),
            responded_at=utcnow(),
        )


# --- Integration Tests (requires PostgreSQL) ---

@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_search_compatibility_export_golden_and_regeneration(tmp_path, prepared_database):
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_svc = build_run_service(config)

    ext_id = f"run-export-{uuid4()}"
    run_svc.create(objective="test compatibility exports", external_id=ext_id)
    status = run_svc.status(external_id=ext_id)
    run_id = status.id

    acq_svc = build_acquisition_service(config, search_adapter=MockSearchAdapter())
    acq_res = acq_svc.execute_search(
        run_id,
        "golden export test query",
        export_scratch=False,
    )

    exporter = build_compatibility_export_service(config)
    target_dir = tmp_path / "export_output"

    # 1. Export search compatibility artifacts
    res1 = exporter.export_search(run_id, acq_res.search_response_id, target_dir)
    assert res1.status == "complete"
    assert res1.source_state_sha256 is not None
    assert len(res1.files_created) == 3

    # Check generated files and golden schema structure
    search_json = json.loads((target_dir / "_search.json").read_text(encoding="utf-8"))
    assert search_json["success"] is True

    cands_json = json.loads((target_dir / "_candidates.json").read_text(encoding="utf-8"))
    assert cands_json["search_response_id"] == str(acq_res.search_response_id)
    assert cands_json["candidate_count"] == 2
    assert cands_json["candidates"][0]["canonical_url"] == "https://export-test.com/item1"
    assert cands_json["candidates"][0]["candidate_id"] is not None

    meta_json = json.loads((target_dir / "_meta.json").read_text(encoding="utf-8"))
    assert meta_json["query"] == "golden export test query"
    assert meta_json["search_response_id"] == str(acq_res.search_response_id)
    assert meta_json["run_id"] == str(run_id)
    assert meta_json["source_state_sha256"] == res1.source_state_sha256
    assert meta_json["export_schema_version"] == 1

    # 2. Deletion & Regeneration Test
    # Delete scratch directory completely
    for file in target_dir.glob("*"):
        file.unlink()
    target_dir.rmdir()
    assert not target_dir.exists()

    # Regenerate search exports from PostgreSQL authority
    regen_results = exporter.regenerate_search_exports(run_id, tmp_path / "regen_output")
    assert len(regen_results) == 1
    res2 = regen_results[0]
    assert res2.status == "complete"
    assert res2.source_state_sha256 == res1.source_state_sha256

    regen_meta = json.loads((tmp_path / "regen_output" / "response_001" / "_meta.json").read_text(encoding="utf-8"))
    assert regen_meta["source_state_sha256"] == res1.source_state_sha256


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_search_compatibility_export_failure_isolation(tmp_path, prepared_database):
    """Export failure must be logged in compatibility_exports without eroding acquisition success in PG."""
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_svc = build_run_service(config)

    ext_id = f"run-export-fail-{uuid4()}"
    run_svc.create(objective="test export failure isolation", external_id=ext_id)
    status = run_svc.status(external_id=ext_id)
    run_id = status.id

    acq_svc = build_acquisition_service(config, search_adapter=MockSearchAdapter())
    acq_res = acq_svc.execute_search(
        run_id,
        "export failure test query",
        export_scratch=False,
    )

    exporter = build_compatibility_export_service(config)

    # Force write failure by using a file path as directory
    invalid_target_dir = tmp_path / "blocker_file_export"
    invalid_target_dir.write_text("blocker file content")

    res = exporter.export_search(run_id, acq_res.search_response_id, invalid_target_dir)

    # Export status should be failed with error string
    assert res.status == "failed"
    assert res.error is not None

    # Prove PostgreSQL acquisition state remains completely intact
    stored_resp = run_svc.get_search_response(acq_res.search_response_id)
    assert stored_resp["status"] == "succeeded"
    cands = run_svc.list_candidates(run_id)
    assert len(cands) == 2
