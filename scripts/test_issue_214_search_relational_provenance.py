"""Regression coverage for issue #214 relational search provenance."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from research_store.config import StoreConfig
from research_store.container import (
    build_acquisition_service,
    build_run_service,
    build_workflow_operation_service,
)
from research_store.domain import SearchAdapterResult, utcnow
from research_store.postgres import connect, migrate, require_disposable_database_reset
from research_store.search_provenance import PlannedAcquisitionService

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
    ) -> None:
        self.payload = payload
        self.attempt = attempt
        self.transport_error = transport_error
        self.cancelled = cancelled
        self.on_search = on_search
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
            },
            requested_at=utcnow(),
            responded_at=utcnow(),
        )


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
def test_three_smart_plan_queries_link_directly_to_plan_rows(
    tmp_path, prepared_database
):
    config = replace(
        StoreConfig.from_env(), database_url=TEST_DSN, blob_root=tmp_path / "blobs"
    )
    run_service, status = _prepared_run(config, "three-smart")
    query_texts = [f"smart query {index}" for index in range(3)]
    plan_id, query_ids = _plan(run_service, status.id, query_texts)
    adapter = _Adapter({"success": True, "data": []})
    delegate = build_acquisition_service(config, search_adapter=adapter)
    service = PlannedAcquisitionService(
        delegate,
        uow_factory=run_service.uow_factory,
        run_id=status.id,
        plan_id=plan_id,
        planned_query_texts=frozenset(query_texts),
    )

    response_ids = [
        service.execute_search(status.id, text).search_response_id
        for text in query_texts
    ]

    with run_service.uow_factory() as uow, uow.connection.cursor() as cur:
        cur.execute(
            """SELECT sr.plan_query_id,pq.status,sr.invocation_id,sr.attempt_ordinal
               FROM search_responses sr
               JOIN search_plan_queries pq ON pq.id=sr.plan_query_id
               WHERE sr.id = ANY(%s)
               ORDER BY pq.query_index""",
            (response_ids,),
        )
        rows = cur.fetchall()
    assert [UUID(str(row[0])) for row in rows] == query_ids
    assert [row[1] for row in rows] == ["empty", "empty", "empty"]
    assert all(row[2] is not None and row[3] == 1 for row in rows)


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


def test_migration_backfill_has_no_fuzzy_historical_matching() -> None:
    migration = (
        Path(__file__).resolve().parent
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
    assert "query_text" not in backfill
    assert "created_at" not in backfill
    assert "requested_at" not in backfill
    assert "responded_at" not in backfill
