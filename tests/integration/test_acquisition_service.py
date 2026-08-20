from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from research_store.acquisition_service import (
    AcquisitionService,
    FirecrawlSearchAdapter,
)
from research_store.config import StoreConfig
from research_store.container import (
    build_acquisition_service,
    build_run_service,
    build_workflow_operation_service,
)
from research_store.domain import SearchAdapterResult, utcnow
from research_store.postgres import connect, migrate, require_disposable_database_reset

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""


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
        self.raw_payload = raw_payload or json.dumps(
            {
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
            }
        ).encode("utf-8")
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
            raw_payload=json.dumps(
                {"success": False, "error": "Network transport error: EAI_AGAIN"}
            ).encode("utf-8"),
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
    payload = json.dumps(
        {"success": True, "data": [{"url": "https://example.com"}]}
    ).encode("utf-8")

    commands = []

    def success_runner(cmd):
        commands.append(cmd)
        return 0, payload, ""

    adapter = FirecrawlSearchAdapter(runner=success_runner)
    res = adapter.search("python programming")
    assert res.http_status == 200
    assert res.transport_error is None
    assert res.raw_payload == payload
    assert commands[0][1] == "search"
    assert "--scrape" not in commands[0]
    assert "--scrape-formats" not in commands[0]


def test_firecrawl_direct_scrape_is_wrapped_as_real_candidate():
    payload = json.dumps(
        {
            "markdown": "# Authoritative source",
            "metadata": {
                "scrapeId": "scrape-1",
                "url": "https://raw.example/source.py",
                "statusCode": 200,
            },
        }
    ).encode()
    commands = []

    def success_runner(cmd):
        commands.append(cmd)
        return 0, payload, ""

    result = FirecrawlSearchAdapter(runner=success_runner).search(
        "https://raw.example/source.py",
        backend="firecrawl_scrape",
    )
    wrapped = json.loads(result.raw_payload)
    assert commands[0][1] == "scrape"
    assert commands[0][commands[0].index("--format") + 1] == "markdown"
    assert wrapped["data"]["web"][0]["markdown"] == "# Authoritative source"
    assert result.provider_request_id == "scrape-1"


def test_execute_search_invalid_query():
    svc = AcquisitionService(uow_factory=lambda: None)
    with pytest.raises(ValueError, match="query_text must be non-empty"):
        svc.execute_search(uuid4(), "   ")


# --- Integration Tests (requires PostgreSQL) ---


def _prepared_run(config: StoreConfig, objective: str, external_id: str):
    run_service = build_run_service(config)
    run_service.create(objective=objective, external_id=external_id)
    build_workflow_operation_service(config).prepare_run(external_id)
    return run_service, run_service.status(external_id=external_id)


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_acquisition_service_normal_flow(tmp_path, prepared_database):
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )

    ext_id = f"run-acq-{uuid4()}"
    run_svc, status = _prepared_run(
        config,
        "test acquisition service",
        ext_id,
    )
    run_id = status.id

    mock_adapter = MockSuccessSearchAdapter()
    acq_svc = build_acquisition_service(config, search_adapter=mock_adapter)

    res = acq_svc.execute_search(
        run_id,
        "machine learning tutorials",
    )

    assert res.postgres_committed is True
    assert res.status == "succeeded"
    assert res.candidate_count == 2
    assert res.event_id is not None
    assert not hasattr(res, "scratch_exported")
    assert not hasattr(res, "scratch_error")

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
def test_acquisition_service_idempotent_retry(tmp_path, prepared_database):
    """Retried search calls with same idempotency_key must not create duplicate candidates or responses."""
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )

    ext_id = f"run-acq-retry-{uuid4()}"
    run_svc, status = _prepared_run(
        config,
        "test search retries",
        ext_id,
    )
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
    assert res2.replayed is True
    assert mock_adapter.call_count == 1

    responses = run_svc.list_search_responses(run_id)
    assert len(responses) == 1

    cands = run_svc.list_candidates(run_id)
    assert len(cands) == 2


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_acquisition_service_conflicting_retry_fails_before_provider(
    tmp_path, prepared_database
):
    from research_store.acquisition_service import AcquisitionIdempotencyConflictError

    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    ext_id = f"run-acq-conflict-{uuid4()}"
    run_svc, status = _prepared_run(
        config,
        "test search idempotency conflict",
        ext_id,
    )
    run_id = status.id
    adapter = MockSuccessSearchAdapter()
    service = build_acquisition_service(config, search_adapter=adapter)
    key = f"key-{uuid4()}"

    service.execute_search(run_id, "first request", idempotency_key=key)
    with pytest.raises(
        AcquisitionIdempotencyConflictError,
        match="another request",
    ):
        service.execute_search(run_id, "different request", idempotency_key=key)

    assert adapter.call_count == 1
    assert len(run_svc.list_search_responses(run_id)) == 1


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_acquisition_service_transport_error_persistence(tmp_path, prepared_database):
    """Transport errors must be recorded in DB with provider_error status."""
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )

    ext_id = f"run-acq-trans-err-{uuid4()}"
    run_svc, status = _prepared_run(
        config,
        "test transport error recording",
        ext_id,
    )
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
    payload = json.dumps(
        {
            "success": True,
            "data": [
                {"url": "https://reconcile.example.com/doc1", "title": "Reconciled Doc"}
            ],
        }
    )
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
