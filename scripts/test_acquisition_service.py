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

from research_store.acquisition_service import AcquisitionService, FirecrawlSearchAdapter
from research_store.config import StoreConfig
from research_store.container import build_acquisition_service, build_run_service
from research_store.domain import SearchAdapterResult, utcnow
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


# --- Mock Search Adapters for Unit Tests ---

class MockSuccessSearchAdapter:
    def __init__(self, raw_payload: bytes | None = None):
        self.raw_payload = raw_payload or json.dumps({
            "success": True,
            "data": [
                {
                    "url": "https://example.com/page1",
                    "title": "Page One",
                    "description": "First search result page",
                },
                {
                    "url": "https://example.org/page2",
                    "title": "Page Two",
                    "description": "Second search result page",
                },
            ],
        }).encode("utf-8")
        self.call_count = 0

    def search(self, query_text: str, **kwargs) -> SearchAdapterResult:
        self.call_count += 1
        return SearchAdapterResult(
            raw_payload=self.raw_payload,
            http_status=200,
            provider_request_id=f"req-{uuid4()}",
            transport_error=None,
            transport_metadata={"mock": True, "call_count": self.call_count},
            requested_at=utcnow(),
            responded_at=utcnow(),
        )


class MockTransportErrorSearchAdapter:
    def search(self, query_text: str, **kwargs) -> SearchAdapterResult:
        return SearchAdapterResult(
            raw_payload=json.dumps({"success": False, "error": "Network transport error: EAI_AGAIN"}).encode("utf-8"),
            http_status=500,
            provider_request_id=None,
            transport_error="Network transport error: EAI_AGAIN",
            transport_metadata={"attempts": 3, "exit_code": 1},
            requested_at=utcnow(),
            responded_at=utcnow(),
        )


# --- Unit Tests ---

def test_firecrawl_search_adapter_transport_error_classification():
    def failing_runner(cmd):
        return 1, b"", "firecrawl: error: getaddrinfo EAI_AGAIN api.firecrawl.dev"

    adapter = FirecrawlSearchAdapter(runner=failing_runner)
    res = adapter.search("python programming", retries=1)
    assert res.http_status == 500
    assert res.transport_error == "Network transport error: EAI_AGAIN"
    assert b"EAI_AGAIN" in res.raw_payload


def test_firecrawl_search_adapter_success_runner():
    payload = json.dumps({"success": True, "data": [{"url": "https://example.com"}]}).encode("utf-8")

    def success_runner(cmd):
        return 0, payload, ""

    adapter = FirecrawlSearchAdapter(runner=success_runner)
    res = adapter.search("python programming")
    assert res.http_status == 200
    assert res.transport_error is None
    assert res.raw_payload == payload


def test_execute_search_invalid_query():
    svc = AcquisitionService(uow_factory=lambda: None)
    with pytest.raises(ValueError, match="query_text must be non-empty"):
        svc.execute_search(uuid4(), "   ")


# --- Integration Tests (requires PostgreSQL) ---

@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_acquisition_service_normal_flow(tmp_path, prepared_database):
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_svc = build_run_service(config)

    ext_id = f"run-acq-{uuid4()}"
    run_svc.create(objective="test acquisition service", external_id=ext_id)
    status = run_svc.status(external_id=ext_id)
    run_id = status.id

    mock_adapter = MockSuccessSearchAdapter()
    acq_svc = build_acquisition_service(config, search_adapter=mock_adapter)

    scratch_dir = tmp_path / "scratch_1"
    res = acq_svc.execute_search(
        run_id,
        "machine learning tutorials",
        scratch_dir=scratch_dir,
        export_scratch=True,
    )

    assert res.postgres_committed is True
    assert res.scratch_exported is True
    assert res.status == "succeeded"
    assert res.candidate_count == 2
    assert res.event_id is not None
    assert res.scratch_error is None

    # Verify scratch files
    assert (scratch_dir / "_search.json").is_file()
    assert (scratch_dir / "_meta.json").is_file()
    meta_json = json.loads((scratch_dir / "_meta.json").read_text(encoding="utf-8"))
    assert meta_json["candidate_count"] == 2
    assert meta_json["query"] == "machine learning tutorials"

    # Verify DB records via run service
    stored_resp = run_svc.get_search_response(res.search_response_id)
    assert stored_resp["query_text"] == "machine learning tutorials"
    assert stored_resp["status"] == "succeeded"
    assert stored_resp["result_count"] == 2

    cands = run_svc.list_candidates(run_id)
    assert len(cands) == 2


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_acquisition_service_scratch_export_failure(tmp_path, prepared_database):
    """Scratch write failure must NOT rollback or erase committed PostgreSQL search state."""
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_svc = build_run_service(config)

    ext_id = f"run-acq-scratch-fail-{uuid4()}"
    run_svc.create(objective="test scratch export failure", external_id=ext_id)
    status = run_svc.status(external_id=ext_id)
    run_id = status.id

    mock_adapter = MockSuccessSearchAdapter()
    acq_svc = build_acquisition_service(config, search_adapter=mock_adapter)

    # Use a file as scratch_dir to force mkdir/write failure
    invalid_scratch_dir = tmp_path / "blocker_file"
    invalid_scratch_dir.write_text("i am a file not a directory")

    res = acq_svc.execute_search(
        run_id,
        "deep learning papers",
        scratch_dir=invalid_scratch_dir,
        export_scratch=True,
    )

    # PostgreSQL commit must still be successful!
    assert res.postgres_committed is True
    assert res.scratch_exported is False
    assert res.scratch_error is not None
    assert res.status == "succeeded"

    # Verify that search response and candidates exist in DB despite scratch failure
    stored_resp = run_svc.get_search_response(res.search_response_id)
    assert stored_resp["query_text"] == "deep learning papers"
    assert stored_resp["status"] == "succeeded"


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_acquisition_service_idempotent_retry(tmp_path, prepared_database):
    """Retried search calls with same idempotency_key must not create duplicate candidates or responses."""
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_svc = build_run_service(config)

    ext_id = f"run-acq-retry-{uuid4()}"
    run_svc.create(objective="test search retries", external_id=ext_id)
    status = run_svc.status(external_id=ext_id)
    run_id = status.id

    mock_adapter = MockSuccessSearchAdapter()
    acq_svc = build_acquisition_service(config, search_adapter=mock_adapter)

    idempotency_key = f"key-{uuid4()}"
    res1 = acq_svc.execute_search(
        run_id,
        "quantum computing overview",
        idempotency_key=idempotency_key,
    )

    res2 = acq_svc.execute_search(
        run_id,
        "quantum computing overview",
        idempotency_key=idempotency_key,
    )

    assert res1.search_response_id == res2.search_response_id
    assert res1.postgres_committed is True
    assert res2.postgres_committed is True

    responses = run_svc.list_search_responses(run_id)
    assert len(responses) == 1

    cands = run_svc.list_candidates(run_id)
    assert len(cands) == 2


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_acquisition_service_transport_error_persistence(tmp_path, prepared_database):
    """Transport errors must be recorded in DB with provider_error status."""
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_svc = build_run_service(config)

    ext_id = f"run-acq-trans-err-{uuid4()}"
    run_svc.create(objective="test transport error recording", external_id=ext_id)
    status = run_svc.status(external_id=ext_id)
    run_id = status.id

    mock_adapter = MockTransportErrorSearchAdapter()
    acq_svc = build_acquisition_service(config, search_adapter=mock_adapter)

    res = acq_svc.execute_search(
        run_id,
        "query causing transport failure",
    )

    assert res.postgres_committed is True
    assert res.status == "provider_error"
    assert res.candidate_count == 0

    stored_resp = run_svc.get_search_response(res.search_response_id)
    assert stored_resp["status"] == "provider_error"
    assert "EAI_AGAIN" in stored_resp["error_message"]


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_acquisition_service_crash_reconciliation(tmp_path, prepared_database):
    """Reconciling pending searches ensures candidate extraction for stored responses."""
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_svc = build_run_service(config)

    ext_id = f"run-acq-reconcile-{uuid4()}"
    run_svc.create(objective="test crash reconciliation", external_id=ext_id)
    status = run_svc.status(external_id=ext_id)
    run_id = status.id

    acq_svc = build_acquisition_service(config)

    # Manually insert a search response without candidates (simulating a crash window)
    payload = json.dumps({
        "success": True,
        "data": [{"url": "https://reconcile.example.com/doc1", "title": "Reconciled Doc"}]
    })
    resp = run_svc.record_search_response(
        run_id,
        "reconciliation query",
        "firecrawl",
        payload,
        f"recon-key-{uuid4()}",
    )

    # Initially candidates list is empty for this candidate URL
    cands_before = run_svc.list_candidates(run_id)
    assert len(cands_before) == 0

    # Run reconciliation
    reconciled = acq_svc.reconcile_pending_searches(run_id)
    assert len(reconciled) >= 1
    assert any(r["search_response_id"] == resp["id"] for r in reconciled)

    # After reconciliation, candidates are extracted
    cands_after = run_svc.list_candidates(run_id)
    assert len(cands_after) == 1
    assert cands_after[0]["original_url"] == "https://reconcile.example.com/doc1"
