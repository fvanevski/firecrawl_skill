"""Regression coverage for issue #214 relational search provenance."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import pytest
from psycopg import sql
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from research_store.acquisition_service import (
    AcquisitionAuthorityChangedError,
    AcquisitionConcurrencyError,
    SearchProvenanceError,
)
from research_store.config import StoreConfig
from research_store.container import (
    build_acquisition_service,
    build_run_service,
    build_workflow_operation_service,
)
from research_store.domain import SearchAdapterResult, utcnow
from research_store.inspection_contract import PageRequest
from research_store.inspection_service import InspectionService
from research_store.postgres import connect, migrate, require_disposable_database_reset
from research_store.search_provenance import (
    PlannedAcquisitionService,
    ProvenanceResumableResearchOrchestrator,
)
from research_store.smart_orchestrator import ResumableResearchOrchestrator

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def prepared_database():
    if not TEST_DSN:
        return
    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    migrate(TEST_DSN)


def _prepared_run(config: StoreConfig, label: str):
    service = build_run_service(config)
    external_id = f"run-issue-214-{label}-{uuid4()}"
    service.create(objective=label, external_id=external_id)
    build_workflow_operation_service(config).prepare_run(external_id)
    return service, service.status(external_id=external_id)


def _plan(
    run_service: Any, run_id: UUID, queries: list[str]
) -> tuple[UUID, list[UUID]]:
    with run_service.uow_factory() as uow:
        spec_id = uow.runs.record_research_spec(
            run_id,
            1,
            "research_spec",
            1,
            {"schema_version": "research-spec-v1", "objective": "issue 214"},
            f"issue214:spec:{run_id}",
        )
        with uow.connection.cursor() as cur:
            cur.execute(
                """INSERT INTO search_plans(
                     run_id,research_spec_id,revision,payload,content_sha256,
                     idempotency_key)
                   VALUES(%s,%s,1,%s::jsonb,%s,%s)
                   RETURNING id""",
                (
                    run_id,
                    spec_id,
                    json.dumps({"schema_version": "search-plan-v1"}),
                    "1" * 64,
                    f"issue214:plan:{run_id}",
                ),
            )
            plan_id = UUID(str(cur.fetchone()[0]))
            query_ids: list[UUID] = []
            for index, text in enumerate(queries):
                cur.execute(
                    """INSERT INTO search_plan_queries(
                         plan_id,run_id,query_index,query_text,facet,
                         expected_contribution,payload)
                       VALUES(%s,%s,%s,%s,'objective','issue 214 evidence',%s::jsonb)
                       RETURNING id""",
                    (
                        plan_id,
                        run_id,
                        index,
                        text,
                        json.dumps({"query": text}),
                    ),
                )
                query_ids.append(UUID(str(cur.fetchone()[0])))
        uow.commit()
    return plan_id, query_ids


class _Adapter:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        attempt: int = 1,
        transport_error: str | None = None,
        cancelled: bool = False,
        on_search: Any | None = None,
        transport_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.payload = payload
        self.attempt = attempt
        self.transport_error = transport_error
        self.cancelled = cancelled
        self.on_search = on_search
        self.transport_metadata = dict(transport_metadata or {})
        self.calls = 0

    def search(self, _query_text: str, **_kwargs: Any) -> SearchAdapterResult:
        self.calls += 1
        if self.on_search is not None:
            self.on_search()
        return SearchAdapterResult(
            raw_payload=json.dumps(self.payload).encode(),
            http_status=500 if self.transport_error else 200,
            provider_request_id=f"issue214-{self.calls}",
            transport_error=self.transport_error,
            transport_metadata={
                "attempts": self.attempt,
                "cancelled": self.cancelled,
                **self.transport_metadata,
            },
            requested_at=utcnow(),
            responded_at=utcnow(),
        )


class _RaisingAdapter:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def search(self, _query_text: str, **_kwargs: Any) -> SearchAdapterResult:
        self.calls += 1
        raise self.error


def _response_provenance(run_service: Any, response_id: UUID) -> dict[str, Any]:
    with run_service.uow_factory() as uow, uow.connection.cursor() as cur:
        cur.execute(
            """SELECT invocation_id,attempt_ordinal,provenance_status,
                      plan_id,plan_query_id,status
               FROM search_responses
               WHERE id=%s""",
            (response_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return {
        "invocation_id": row[0],
        "attempt_ordinal": row[1],
        "provenance_status": row[2],
        "plan_id": row[3],
        "plan_query_id": row[4],
        "status": row[5],
    }


def _dsn_for_database(dsn: str, database: str) -> str:
    parsed = urlsplit(dsn)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, f"/{database}", parsed.query, parsed.fragment)
    )


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_direct_and_retried_searches_have_relational_invocation_provenance(
    tmp_path, prepared_database
):
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_service, status = _prepared_run(config, "direct")
    adapter = _Adapter(
        {"success": True, "data": [{"url": "https://example.test/direct"}]},
        attempt=3,
    )
    service = build_acquisition_service(config, search_adapter=adapter)
    key = f"issue214:direct:{uuid4()}"

    first = service.execute_search(status.id, "direct provenance", idempotency_key=key)
    replay = service.execute_search(status.id, "direct provenance", idempotency_key=key)

    assert replay.replayed is True
    assert adapter.calls == 1
    assert replay.search_response_id == first.search_response_id
    stored = _response_provenance(run_service, first.search_response_id)
    assert stored["invocation_id"] == first.invocation_id
    assert stored["attempt_ordinal"] == 3
    assert stored["provenance_status"] == "resolved"
    assert stored["plan_id"] is None
    assert stored["plan_query_id"] is None

    inspector = InspectionService(config)
    history = inspector.list_search_responses(status.id, PageRequest(limit=10))
    history_row = next(
        row for row in history["items"] if row["id"] == str(first.search_response_id)
    )
    assert history_row["invocation_id"] == str(first.invocation_id)
    replayed = inspector.replay_search(first.search_response_id)
    assert replayed["response"]["invocation_id"] == str(first.invocation_id)
    assert "invocation_id" not in replayed["response"]["transport_metadata"]


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
@pytest.mark.parametrize(
    ("payload", "transport_error", "cancelled", "expected_response", "expected_query"),
    [
        (
            {"success": True, "data": [{"url": "https://example.test/result"}]},
            None,
            False,
            "succeeded",
            "succeeded",
        ),
        ({"success": True, "data": []}, None, False, "empty", "empty"),
        (
            {"success": False, "error": "provider failed"},
            "provider failed",
            False,
            "provider_error",
            "failed",
        ),
        (
            {"success": False, "error": "cancelled"},
            "cancelled",
            True,
            "provider_error",
            "cancelled",
        ),
    ],
)
def test_planned_query_running_and_terminal_states_are_transactional(
    tmp_path,
    prepared_database,
    payload,
    transport_error,
    cancelled,
    expected_response,
    expected_query,
):
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_service, status = _prepared_run(config, expected_query)
    query_text = f"planned {expected_query}"
    plan_id, query_ids = _plan(run_service, status.id, [query_text])
    query_id = query_ids[0]

    def assert_running() -> None:
        with run_service.uow_factory() as uow, uow.connection.cursor() as cur:
            cur.execute(
                "SELECT status FROM search_plan_queries WHERE id=%s",
                (query_id,),
            )
            assert cur.fetchone()[0] == "running"

    adapter = _Adapter(
        payload,
        transport_error=transport_error,
        cancelled=cancelled,
        on_search=assert_running,
    )
    delegate = build_acquisition_service(config, search_adapter=adapter)
    service = PlannedAcquisitionService(
        delegate,
        uow_factory=run_service.uow_factory,
        run_id=status.id,
        plan_id=plan_id,
        planned_query_texts=frozenset({query_text}),
    )

    result = service.execute_search(status.id, query_text)

    stored = _response_provenance(run_service, result.search_response_id)
    assert stored["status"] == expected_response
    assert stored["provenance_status"] == "resolved"
    assert stored["invocation_id"] == result.invocation_id
    assert stored["plan_id"] == plan_id
    assert stored["plan_query_id"] == query_id
    with run_service.uow_factory() as uow, uow.connection.cursor() as cur:
        cur.execute("SELECT status FROM search_plan_queries WHERE id=%s", (query_id,))
        assert cur.fetchone()[0] == expected_query


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_production_provenance_orchestrator_links_three_smart_plan_queries(
    tmp_path, prepared_database, monkeypatch
):
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_service, status = _prepared_run(config, "three-smart-production-seam")
    query_texts = [f"smart production query {index}" for index in range(3)]
    plan_id, query_ids = _plan(run_service, status.id, query_texts)
    adapter = _Adapter({"success": True, "data": []})
    delegate = build_acquisition_service(config, search_adapter=adapter)

    orchestrator = object.__new__(ProvenanceResumableResearchOrchestrator)
    orchestrator.run_service = run_service
    orchestrator._acquisition = SimpleNamespace(acquisition_service=delegate)
    orchestrator.acquisition_service = delegate

    def execute_parent_seam(
        self,
        run_id,
        _spec,
        search_plan,
        *,
        max_adaptive_cycles=None,
        context=None,
    ):
        assert max_adaptive_cycles == 1
        assert context["search_plan_id"] == str(plan_id)
        return [
            self.acquisition_service.execute_search(run_id, item["query"])
            for item in search_plan["queries"]
        ]

    monkeypatch.setattr(ResumableResearchOrchestrator, "run", execute_parent_seam)
    results = orchestrator.run(
        status.id,
        {"schema_version": "research-spec-v1"},
        {"queries": [{"query": text} for text in query_texts]},
        max_adaptive_cycles=1,
        context={"search_plan_id": str(plan_id)},
    )

    response_ids = [result.search_response_id for result in results]
    with run_service.uow_factory() as uow, uow.connection.cursor() as cur:
        cur.execute(
            """SELECT sr.plan_id,sr.plan_query_id,pq.status,
                      sr.invocation_id,sr.attempt_ordinal,sr.provenance_status
               FROM search_responses sr
               JOIN search_plan_queries pq
                 ON pq.id=sr.plan_query_id AND pq.run_id=sr.run_id
               WHERE sr.id = ANY(%s)
               ORDER BY pq.query_index""",
            (response_ids,),
        )
        rows = cur.fetchall()
    assert [UUID(str(row[0])) for row in rows] == [plan_id] * 3
    assert [UUID(str(row[1])) for row in rows] == query_ids
    assert [row[2] for row in rows] == ["empty", "empty", "empty"]
    assert all(row[3] is not None and row[4] == 1 for row in rows)
    assert [row[5] for row in rows] == ["resolved", "resolved", "resolved"]


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_authority_change_after_provider_return_terminalizes_running_attempt(
    tmp_path, prepared_database
):
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_service, status = _prepared_run(config, "authority-race")
    query_text = "planned authority race"
    plan_id, query_ids = _plan(run_service, status.id, [query_text])
    query_id = query_ids[0]

    def cancel_during_provider() -> None:
        current = run_service.status(run_id=status.id)
        run_service.cancel(
            status.id,
            expected_revision=current.lifecycle_revision,
            idempotency_key=f"issue214:cancel:{status.id}",
            actor_type="test",
            reason="concurrent cancellation at provider seam",
        )

    adapter = _Adapter(
        {"success": True, "data": []},
        on_search=cancel_during_provider,
    )
    service = build_acquisition_service(config, search_adapter=adapter)

    with pytest.raises(AcquisitionAuthorityChangedError):
        service.execute_search(
            status.id,
            query_text,
            plan_id=plan_id,
            plan_query_id=query_id,
        )

    with run_service.uow_factory() as uow, uow.connection.cursor() as cur:
        cur.execute("SELECT status FROM search_plan_queries WHERE id=%s", (query_id,))
        assert cur.fetchone()[0] == "cancelled"
        cur.execute(
            """SELECT id,status,output,error
               FROM research_invocations
               WHERE run_id=%s AND operation='search_provider'
               ORDER BY created_at DESC LIMIT 1""",
            (status.id,),
        )
        invocation_id, invocation_status, output, error = cur.fetchone()
        assert invocation_status == "cancelled"
        assert output["reason_code"] == "provider_attempt_cancelled"
        assert "concurrent cancellation" not in (error or "")
        cur.execute(
            "SELECT count(*) FROM search_responses WHERE run_id=%s",
            (status.id,),
        )
        assert cur.fetchone()[0] == 0
    assert invocation_id is not None


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_invalid_explicit_attempt_metadata_fails_closed_and_terminalizes(
    tmp_path, prepared_database
):
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_service, status = _prepared_run(config, "invalid-attempt")
    query_text = "planned invalid attempt"
    plan_id, query_ids = _plan(run_service, status.id, [query_text])
    query_id = query_ids[0]
    adapter = _Adapter({"success": True, "data": []}, attempt=2_147_483_648)
    service = build_acquisition_service(config, search_adapter=adapter)

    with pytest.raises(SearchProvenanceError, match="positive 32-bit integer"):
        service.execute_search(
            status.id,
            query_text,
            plan_id=plan_id,
            plan_query_id=query_id,
        )

    with run_service.uow_factory() as uow, uow.connection.cursor() as cur:
        cur.execute("SELECT status FROM search_plan_queries WHERE id=%s", (query_id,))
        assert cur.fetchone()[0] == "failed"
        cur.execute(
            """SELECT status,output
               FROM research_invocations
               WHERE run_id=%s AND operation='search_provider'
               ORDER BY created_at DESC LIMIT 1""",
            (status.id,),
        )
        invocation_status, output = cur.fetchone()
        assert invocation_status == "failed"
        assert output["reason_code"] == "provider_attempt_failed_without_response"
        cur.execute(
            "SELECT count(*) FROM search_responses WHERE run_id=%s",
            (status.id,),
        )
        assert cur.fetchone()[0] == 0


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_provider_exception_error_is_redacted_before_persistence(
    tmp_path, prepared_database
):
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_service, status = _prepared_run(config, "secret-redaction")
    adapter = _RaisingAdapter(
        RuntimeError("token=supersecret Authorization=abc Bearer xyz.123")
    )
    service = build_acquisition_service(config, search_adapter=adapter)

    with pytest.raises(RuntimeError, match="supersecret"):
        service.execute_search(status.id, "redact provider exception")

    with run_service.uow_factory() as uow, uow.connection.cursor() as cur:
        cur.execute(
            """SELECT status,error,output
               FROM research_invocations
               WHERE run_id=%s AND operation='search_provider'
               ORDER BY created_at DESC LIMIT 1""",
            (status.id,),
        )
        invocation_status, error, output = cur.fetchone()
    assert invocation_status == "failed"
    assert "supersecret" not in error
    assert "xyz.123" not in error
    assert "[REDACTED]" in error
    assert output["reason_code"] == "provider_attempt_failed_without_response"


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_provider_response_diagnostics_are_redacted_before_persistence(
    tmp_path, prepared_database
):
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_service, status = _prepared_run(config, "diagnostic-redaction")
    adapter = _Adapter(
        {"success": False, "error": "provider failed"},
        transport_error="token=response-secret Bearer response.jwt",
        transport_metadata={
            "stderr": "authorization=metadata-secret",
            "nested": {"detail": "password=nested-secret"},
        },
    )
    service = build_acquisition_service(config, search_adapter=adapter)

    result = service.execute_search(status.id, "redact provider diagnostics")
    assert result.status == "provider_error"
    with run_service.uow_factory() as uow, uow.connection.cursor() as cur:
        cur.execute(
            """SELECT error_message,transport_metadata
               FROM search_responses WHERE id=%s""",
            (result.search_response_id,),
        )
        error_message, transport_metadata = cur.fetchone()
        cur.execute(
            "SELECT error FROM research_invocations WHERE id=%s",
            (result.invocation_id,),
        )
        invocation_error = cur.fetchone()[0]
    serialized = json.dumps(transport_metadata, sort_keys=True)
    for secret in (
        "response-secret",
        "response.jwt",
        "metadata-secret",
        "nested-secret",
    ):
        assert secret not in (error_message or "")
        assert secret not in (invocation_error or "")
        assert secret not in serialized
    assert "[REDACTED]" in error_message
    assert "[REDACTED]" in serialized


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_idempotency_contention_has_bounded_wait_and_no_second_provider_call(
    tmp_path, prepared_database
):
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    _run_service, status = _prepared_run(config, "bounded-idempotency-lock")
    entered = threading.Event()
    release = threading.Event()

    def block_first_provider() -> None:
        entered.set()
        assert release.wait(timeout=5)

    first_adapter = _Adapter(
        {"success": True, "data": []},
        on_search=block_first_provider,
    )
    second_adapter = _Adapter({"success": True, "data": []})
    first_service = build_acquisition_service(config, search_adapter=first_adapter)
    second_service = build_acquisition_service(config, search_adapter=second_adapter)
    second_service.idempotency_lock_timeout_seconds = 0.15
    second_service.idempotency_lock_poll_seconds = 0.01
    key = f"issue214:bounded-lock:{uuid4()}"

    with ThreadPoolExecutor(max_workers=1) as executor:
        first_future = executor.submit(
            first_service.execute_search,
            status.id,
            "bounded lock",
            idempotency_key=key,
        )
        assert entered.wait(timeout=2)
        started = time.monotonic()
        with pytest.raises(AcquisitionConcurrencyError) as timeout:
            second_service.execute_search(
                status.id,
                "bounded lock",
                idempotency_key=key,
            )
        elapsed = time.monotonic() - started
        assert timeout.value.reason_code == "search_idempotency_lock_timeout"
        assert elapsed < 1.0
        assert second_adapter.calls == 0

        release.set()
        first_result = first_future.result(timeout=5)

    assert first_result.postgres_committed is True
    assert first_adapter.calls == 1


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_constraints_reject_orphaned_and_duplicate_resolved_attempts(
    tmp_path, prepared_database
):
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_service, status = _prepared_run(config, "constraints")
    adapter = _Adapter({"success": True, "data": []})
    service = build_acquisition_service(config, search_adapter=adapter)
    result = service.execute_search(status.id, "constraint source")
    stored = _response_provenance(run_service, result.search_response_id)

    with (
        pytest.raises(ForeignKeyViolation) as orphan,
        run_service.uow_factory() as uow,
        uow.connection.cursor() as cur,
    ):
        cur.execute(
            """INSERT INTO search_responses(
                 run_id,query_text,backend,status,parser_version,
                 raw_blob_sha256,raw_blob_bytes,mime_type,content_sha256,
                 result_count,transport_metadata,payload_summary,idempotency_key,
                 invocation_id,attempt_ordinal,provenance_status)
               SELECT run_id,'orphan',backend,status,parser_version,
                      raw_blob_sha256,raw_blob_bytes,mime_type,content_sha256,
                      result_count,transport_metadata,payload_summary,%s,
                      %s,1,'resolved'
               FROM search_responses WHERE id=%s""",
            (f"orphan:{uuid4()}", uuid4(), result.search_response_id),
        )
        uow.commit()
    assert "search_responses_invocation_run_fk" in str(orphan.value)

    with (
        pytest.raises(UniqueViolation) as duplicate,
        run_service.uow_factory() as uow,
        uow.connection.cursor() as cur,
    ):
        cur.execute(
            """INSERT INTO search_responses(
                 run_id,query_text,backend,status,parser_version,
                 raw_blob_sha256,raw_blob_bytes,mime_type,content_sha256,
                 result_count,transport_metadata,payload_summary,idempotency_key,
                 invocation_id,attempt_ordinal,provenance_status)
               SELECT run_id,'duplicate',backend,status,parser_version,
                      raw_blob_sha256,raw_blob_bytes,mime_type,content_sha256,
                      result_count,transport_metadata,payload_summary,%s,
                      invocation_id,attempt_ordinal,'resolved'
               FROM search_responses WHERE id=%s""",
            (f"duplicate:{uuid4()}", result.search_response_id),
        )
        uow.commit()
    assert "search_responses_invocation_attempt_uidx" in str(duplicate.value)
    assert stored["provenance_status"] == "resolved"


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_migration_safely_classifies_uncertain_and_out_of_range_history():
    database = f"firecrawl_issue214_migration_{uuid4().hex}"
    admin_dsn = _dsn_for_database(TEST_DSN, "postgres")
    isolated_dsn = _dsn_for_database(TEST_DSN, database)
    with connect(admin_dsn) as admin:
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
            )
    try:
        assert migrate(isolated_dsn, "0040_asset_promotion_membership") == 40
        run_id = uuid4()
        other_run_id = uuid4()
        invocation_id = uuid4()
        other_invocation_id = uuid4()
        digest = "a" * 64
        content_digest = "b" * 64
        cases = [
            ("valid", str(invocation_id), "1", None),
            ("overflow", str(invocation_id), "2147483648", None),
            ("huge", str(invocation_id), "9" * 200, None),
            ("bad-uuid", "not-a-uuid", "2", None),
            ("wrong-run", str(other_invocation_id), "3", None),
            ("ambiguous-a", str(invocation_id), "4", None),
            ("ambiguous-b", str(invocation_id), "4", None),
            ("conflicting", str(invocation_id), "5", "6"),
        ]
        with connect(isolated_dsn) as connection, connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs
                   (id,objective,state,execution_mode,external_run_id)
                   VALUES(%s,'issue214 migration','created','agent_led',%s),
                         (%s,'issue214 other','created','agent_led',%s)""",
                (
                    run_id,
                    f"fr_{run_id.hex}",
                    other_run_id,
                    f"fr_{other_run_id.hex}",
                ),
            )
            cur.execute(
                """INSERT INTO research_invocations
                   (id,run_id,operation,status,lifecycle_revision,idempotency_key,input)
                   VALUES(%s,%s,'search_provider','complete',0,%s,'{}'),
                         (%s,%s,'search_provider','complete',0,%s,'{}')""",
                (
                    invocation_id,
                    run_id,
                    f"issue214:invocation:{invocation_id}",
                    other_invocation_id,
                    other_run_id,
                    f"issue214:invocation:{other_invocation_id}",
                ),
            )
            for label, metadata_invocation, attempt, attempts in cases:
                metadata = {
                    "invocation_id": metadata_invocation,
                    "attempt": attempt,
                }
                if attempts is not None:
                    metadata["attempts"] = attempts
                cur.execute(
                    """INSERT INTO search_responses(
                         run_id,query_text,backend,status,parser_version,
                         raw_blob_sha256,raw_blob_bytes,mime_type,content_sha256,
                         result_count,transport_metadata,payload_summary,
                         idempotency_key)
                       VALUES(%s,%s,'firecrawl','empty','legacy-v1',%s,0,
                              'application/json',%s,0,%s::jsonb,'{}',%s)""",
                    (
                        run_id,
                        f"legacy {label}",
                        digest,
                        content_digest,
                        json.dumps(metadata),
                        f"issue214:legacy:{label}",
                    ),
                )
            connection.commit()

        assert migrate(isolated_dsn) == 44
        with connect(isolated_dsn) as connection, connection.cursor() as cur:
            cur.execute(
                """SELECT idempotency_key,invocation_id,attempt_ordinal,
                          provenance_status
                   FROM search_responses
                   WHERE run_id=%s
                   ORDER BY idempotency_key""",
                (run_id,),
            )
            rows = {row[0].rsplit(":", 1)[-1]: row[1:] for row in cur.fetchall()}
        assert rows["valid"] == (invocation_id, 1, "resolved")
        for label in (
            "overflow",
            "huge",
            "bad-uuid",
            "wrong-run",
            "ambiguous-a",
            "ambiguous-b",
            "conflicting",
        ):
            assert rows[label] == (None, None, "historical_unresolved")
    finally:
        with connect(admin_dsn) as admin:
            admin.autocommit = True
            with admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(database)
                    )
                )


def test_migration_backfill_has_no_fuzzy_historical_matching() -> None:
    migration = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "research_store"
        / "alembic"
        / "versions"
        / "0041_search_relational_provenance.py"
    ).read_text(encoding="utf-8")
    backfill = migration.split("WITH parsed_metadata AS", 1)[1].split(
        "ALTER TABLE search_responses", 1
    )[0]
    assert "transport_metadata" in backfill
    assert "research_invocations" in backfill
    assert "::numeric <= 2147483647" in backfill
    assert "query_text" not in backfill
    assert "created_at" not in backfill
    assert "requested_at" not in backfill
    assert "responded_at" not in backfill
