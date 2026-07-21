from __future__ import annotations

from dataclasses import replace
import hashlib
import io

import json
import os
from pathlib import Path
import sys
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.blob import ContentAddressedBlobStore
from research_store.config import StoreConfig
from research_store.container import build_run_service
from research_store.domain import utcnow
from research_store.parsing import parse_raw_search_response
from research_store.postgres import connect, migrate, require_disposable_database_reset
from research_store.replay import SearchResponseReplayReader


TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def prepared_database():
    """Prove fresh and populated prior-head migrations without data loss."""
    if not TEST_DSN:
        return
    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    assert migrate(TEST_DSN) >= 10




# --- Unit Tests ---

def test_parse_raw_search_response_succeeded():
    raw = json.dumps({"success": True, "data": [{"url": "https://example.com", "title": "Example"}]})
    status, count, summary, err = parse_raw_search_response(raw)
    assert status == "succeeded"
    assert count == 1
    assert summary["result_count"] == 1
    assert summary["sample_candidates"][0]["url"] == "https://example.com"
    assert err is None


def test_parse_raw_search_response_empty():
    raw = json.dumps({"success": True, "data": []})
    status, count, summary, err = parse_raw_search_response(raw)
    assert status == "empty"
    assert count == 0
    assert summary["result_count"] == 0
    assert err is None


def test_parse_raw_search_response_provider_error():
    # Via payload failure flag
    raw1 = json.dumps({"success": False, "error": "Rate limit exceeded"})
    status1, count1, summary1, err1 = parse_raw_search_response(raw1)
    assert status1 == "provider_error"
    assert count1 == 0
    assert err1 == "Rate limit exceeded"

    # Via HTTP status code >= 400
    raw2 = json.dumps({"detail": "Server error"})
    status2, count2, summary2, err2 = parse_raw_search_response(raw2, http_status=500)
    assert status2 == "provider_error"
    assert count2 == 0
    assert err2 == "Server error"


def test_parse_raw_search_response_parse_error():
    raw = "<html>502 Bad Gateway</html>"
    status, count, summary, err = parse_raw_search_response(raw)
    assert status == "parse_error"
    assert count == 0
    assert "Failed to parse search response as JSON" in err


def test_replay_reader_integrity_and_missing_blob(tmp_path):
    blob_store = ContentAddressedBlobStore(tmp_path / "blobs")
    payload = b'{"success": true, "data": []}'
    blob_ref = blob_store.put(io.BytesIO(payload))

    class MockRepo:
        def get_search_response(self, response_id, run_id=None):
            return {
                "id": response_id,
                "run_id": uuid4(),
                "plan_id": None,
                "plan_query_id": None,
                "query_text": "test query",
                "backend": "firecrawl",
                "provider_request_id": "req-1",
                "status": "empty",
                "http_status": 200,
                "parser_version": "firecrawl-search-v1",
                "raw_blob_sha256": blob_ref.sha256,
                "content_sha256": blob_ref.sha256,
                "result_count": 0,
                "error_message": None,
                "transport_metadata": {},
                "payload_summary": {},
                "idempotency_key": "key-1",
            }

    reader = SearchResponseReplayReader(MockRepo(), blob_store)
    resp_id = uuid4()
    replay = reader.replay_search_response(resp_id)

    assert replay.id == resp_id
    assert replay.raw_bytes == payload
    assert replay.verify_integrity() is True
    assert replay.status == "empty"

    # Missing blob test
    missing_store = ContentAddressedBlobStore(tmp_path / "empty_blobs")
    missing_reader = SearchResponseReplayReader(MockRepo(), missing_store)
    with pytest.raises(FileNotFoundError, match="not found in blob store"):
        missing_reader.replay_search_response(resp_id)


# --- Integration Tests (requires PostgreSQL) ---

@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_record_search_response_integration(tmp_path, prepared_database):
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )

    run_svc = build_run_service(config)

    ext_id = f"run-search-resp-{uuid4()}"
    run_svc.create(objective="test search responses", external_id=ext_id)
    status = run_svc.status(external_id=ext_id)
    run_id = status.id

    raw_payload = json.dumps(
        {
            "success": True,
            "data": [
                {"url": "https://example.com/1", "title": "Result 1"},
                {"url": "https://example.com/2", "title": "Result 2"},
            ],
        }
    )

    rec = run_svc.record_search_response(
        run_id,
        query_text="python search response persistence",
        backend="firecrawl",
        raw_payload=raw_payload,
        idempotency_key="idemp-resp-1",
        provider_request_id="fc-req-100",
        http_status=200,
    )

    assert rec["run_id"] == run_id
    assert rec["query_text"] == "python search response persistence"
    assert rec["status"] == "succeeded"
    assert rec["result_count"] == 2
    assert rec["backend"] == "firecrawl"
    assert rec["provider_request_id"] == "fc-req-100"
    assert rec["content_sha256"] == hashlib.sha256(raw_payload.encode()).hexdigest()

    # Verify blob file on disk
    blob_store = ContentAddressedBlobStore(tmp_path / "blobs")
    assert blob_store.exists(rec["raw_blob_sha256"]) is True

    # Get search response
    fetched = run_svc.get_search_response(rec["id"])
    assert fetched["id"] == rec["id"]
    assert fetched["query_text"] == rec["query_text"]

    # Replay search response
    replay = run_svc.replay_search_response(rec["id"])
    assert replay.verify_integrity() is True
    assert replay.result_count == 2
    assert isinstance(replay.parsed_json, dict)
    assert len(replay.parsed_json["data"]) == 2


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_idempotent_duplicate_search_response_integration(tmp_path, prepared_database):
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )

    run_svc = build_run_service(config)

    ext_id = f"run-idemp-resp-{uuid4()}"
    run_svc.create(objective="test idempotency", external_id=ext_id)
    run_id = run_svc.status(external_id=ext_id).id

    raw_payload = json.dumps({"success": True, "data": []})
    idempotency_key = f"key-idemp-{uuid4()}"

    rec1 = run_svc.record_search_response(
        run_id,
        query_text="duplicate search query",
        backend="firecrawl",
        raw_payload=raw_payload,
        idempotency_key=idempotency_key,
    )

    # Re-submitting identical payload with same idempotency key returns existing record
    rec2 = run_svc.record_search_response(
        run_id,
        query_text="duplicate search query",
        backend="firecrawl",
        raw_payload=raw_payload,
        idempotency_key=idempotency_key,
    )
    assert rec1["id"] == rec2["id"]

    # Submitting different payload with same idempotency key raises ValueError
    different_payload = json.dumps({"success": True, "data": [{"url": "https://different.org"}]})
    with pytest.raises(ValueError, match="idempotency_key conflict"):
        run_svc.record_search_response(
            run_id,
            query_text="duplicate search query",
            backend="firecrawl",
            raw_payload=different_payload,
            idempotency_key=idempotency_key,
        )


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_blob_orphan_on_transaction_rollback_integration(tmp_path, prepared_database):
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )

    run_svc = build_run_service(config)

    ext_id = f"run-orphan-{uuid4()}"
    run_svc.create(objective="test blob orphan", external_id=ext_id)
    run_id = run_svc.status(external_id=ext_id).id

    raw_payload = json.dumps({"test": "orphan payload"})
    payload_sha = hashlib.sha256(raw_payload.encode()).hexdigest()
    blob_store = ContentAddressedBlobStore(tmp_path / "blobs")

    # Simulate transaction rollback after blob write
    with run_svc.uow_factory() as uow:
        try:
            with uow.savepoint():
                uow.runs.record_search_response(
                    run_id,
                    query_text="orphan search query",
                    backend="firecrawl",
                    raw_payload=raw_payload,
                    idempotency_key=f"orphan-key-{uuid4()}",
                    blob_store=blob_store,
                )
                # Explicitly force an exception inside savepoint to trigger rollback
                raise ValueError("simulated transaction failure")
        except ValueError:
            pass


    # DB should have 0 responses for run_id
    assert len(run_svc.list_search_responses(run_id)) == 0

    # Blob store SHOULD still contain the written blob file on disk as an unreferenced orphan
    assert blob_store.exists(payload_sha) is True

    # Submitting again in a committed transaction succeeds and references the existing blob
    rec = run_svc.record_search_response(
        run_id,
        query_text="orphan search query retry",
        backend="firecrawl",
        raw_payload=raw_payload,
        idempotency_key=f"orphan-key-retry-{uuid4()}",
        blob_store=blob_store,
    )
    assert rec["raw_blob_sha256"] == payload_sha
    assert len(run_svc.list_search_responses(run_id)) == 1
