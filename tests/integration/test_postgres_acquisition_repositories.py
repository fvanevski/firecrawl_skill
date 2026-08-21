"""Focused regressions for issue #258 PostgreSQL acquisition repositories."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from firecrawl_skill.research_store.blob import ContentAddressedBlobStore
from firecrawl_skill.research_store.candidate_policy_service import (
    CandidatePolicyService,
)
from firecrawl_skill.research_store.domain import utcnow
from firecrawl_skill.research_store.postgres import PostgresUnitOfWork, connect, migrate
from firecrawl_skill.research_store.postgres_acquisition import (
    PostgresCandidateRepository,
    PostgresExtractionAttemptRepository,
    PostgresSearchAcquisitionRepository,
)

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
INTEGRATION = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


class _FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeConnection:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.transactions = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closes += 1

    def transaction(self) -> _FakeTransaction:
        self.transactions += 1
        return _FakeTransaction()


def test_candidate_policy_ranking_delegate_is_not_import_time_rebound():
    """Runtime policy routing must remain identical to the class source."""
    method = CandidatePolicyService.record_rankings
    assert (
        method.__module__ == "firecrawl_skill.research_store.candidate_policy_service"
    )
    assert not hasattr(method, "__wrapped__")
    assert not hasattr(CandidatePolicyService, "_issue_258_repository_installed")


def test_acquisition_roles_bind_canonical_repositories_on_exact_uow_connection(
    monkeypatch,
):
    connection = _FakeConnection()
    monkeypatch.setattr(
        "firecrawl_skill.research_store.postgres.connect",
        lambda _database_url: connection,
    )

    with PostgresUnitOfWork("postgresql://test.invalid/db", "test-index") as uow:
        assert uow.search_responses.connection_identity == id(connection)
        assert uow.candidates.connection_identity == id(connection)
        assert uow.extraction_attempts.connection_identity == id(connection)

        assert isinstance(
            uow.search_responses.record_search_response.__self__,
            PostgresSearchAcquisitionRepository,
        )
        assert isinstance(
            uow.candidates.record_response_candidates.__self__,
            PostgresCandidateRepository,
        )
        assert isinstance(
            uow.candidates.record_rankings.__self__,
            PostgresCandidateRepository,
        )
        assert isinstance(
            uow.extraction_attempts.create_attempt.__self__,
            PostgresExtractionAttemptRepository,
        )

        for repository in (
            uow.search_responses,
            uow.candidates,
            uow.extraction_attempts,
        ):
            for capability in (
                "connection",
                "commit",
                "rollback",
                "savepoint",
                "execute",
                "fetchone",
            ):
                assert not hasattr(repository, capability)

        # The final topology intentionally rejects the historical cross-domain
        # acquisition/candidate router through uow.runs.
        with pytest.raises(AttributeError):
            _ = uow.runs.record_search_response
        with pytest.raises(AttributeError):
            _ = uow.runs.record_response_candidates

    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closes == 1


def _start_run(uow: PostgresUnitOfWork, suffix: str) -> UUID:
    return uow.runs.start_run(
        f"issue-258 {suffix}",
        {
            "external_run_id": f"issue-258-{suffix}-{uuid4()}",
            "execution_mode": "agent_led",
            "metadata": {"test": "issue-258"},
        },
    )


def _response_payload() -> bytes:
    return json.dumps(
        {
            "success": True,
            "data": {
                "web": [
                    {
                        "url": "https://example.com/issue-258/a",
                        "title": "Issue 258 A",
                        "description": "first acquisition candidate",
                    },
                    {
                        "url": "https://example.org/issue-258/b",
                        "title": "Issue 258 B",
                        "description": "second acquisition candidate",
                    },
                ]
            },
        }
    ).encode()


def _ranking_rows(candidate_ids: list[UUID]) -> list[dict]:
    rows = []
    for ordinal, candidate_id in enumerate(candidate_ids):
        selected = ordinal == 0
        rows.append(
            {
                "candidate_id": candidate_id,
                "source_rank": ordinal + 1,
                "url": (
                    "https://example.com/issue-258/a"
                    if selected
                    else "https://example.org/issue-258/b"
                ),
                "url_type": "article",
                "base_score": 1.0 if selected else 0.5,
                "url_type_penalty": 0.0,
                "freshness_status": "not_applicable",
                "freshness_penalty": 0.0,
                "is_duplicate": False,
                "duplication_penalty": 0.0,
                "expected_char_count": None,
                "size_penalty": 0.0,
                "total_score": 1.0 if selected else 0.5,
                "rationale": f"issue #258 regression candidate {ordinal}",
                "decision": "selected" if selected else "rejected",
                "selected_ordinal": 0 if selected else None,
                "decision_reason": (
                    "selected ordinal=0 within scrape_limit=1"
                    if selected
                    else "rejected outside scrape_limit=1"
                ),
            }
        )
    return rows


@INTEGRATION
def test_acquisition_repository_writes_share_one_outer_rollback(tmp_path):
    from psycopg import sql

    migrate(TEST_DSN)
    blob_store = ContentAddressedBlobStore(tmp_path / "blobs")
    run_id = response_id = invocation_id = attempt_id = None
    candidate_ids: list[UUID] = []

    with (
        pytest.raises(RuntimeError, match="force acquisition rollback"),
        PostgresUnitOfWork(TEST_DSN, "issue-258-test-index") as uow,
    ):
        identities = {
            uow.search_responses.connection_identity,
            uow.candidates.connection_identity,
            uow.extraction_attempts.connection_identity,
            id(uow.connection),
        }
        assert len(identities) == 1

        run_id = _start_run(uow, "rollback")
        invocation_id = uow.runs.record_invocation(
            run_id,
            "search_provider",
            f"issue-258-invocation-{uuid4()}",
            status="running",
        )
        response = uow.search_responses.record_search_response(
            run_id,
            "issue 258 acquisition rollback",
            "firecrawl",
            _response_payload(),
            f"issue-258-response-{uuid4()}",
            blob_store,
        )
        response_id = UUID(str(response["id"]))
        candidates = uow.candidates.record_response_candidates(
            run_id, response_id, blob_store
        )
        candidate_ids = [UUID(str(item["candidate_id"])) for item in candidates]
        assert len(candidate_ids) == 2

        uow.candidates.record_rankings(
            run_id,
            response_id,
            invocation_id,
            _ranking_rows(candidate_ids),
        )
        attempt_id = uow.extraction_attempts.create_attempt(
            candidate_id=candidate_ids[0],
            run_id=run_id,
            invocation_id=invocation_id,
            attempt_number=1,
            method="firecrawl_main_content",
            method_version="issue-258-test",
            requested_format="markdown",
            start_time=utcnow(),
            end_time=None,
            exit_status="failed",
            http_status=500,
            backend_status="provider_error",
            raw_blob=None,
            normalized_blob=None,
            parser_used=None,
            quality_metrics=None,
            failure_class="http_error",
            retry_parent_id=None,
            disposition="poor",
            error_message="expected rollback fixture",
            selection_reason=None,
        )
        raise RuntimeError("force acquisition rollback")

    assert all(
        value is not None for value in (run_id, response_id, invocation_id, attempt_id)
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        for table, identifier in (
            ("research_runs", run_id),
            ("research_invocations", invocation_id),
            ("search_responses", response_id),
            ("extraction_attempts", attempt_id),
        ):
            cursor.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE id=%s").format(
                    sql.Identifier(table)
                ),
                (identifier,),
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == 0
        cursor.execute(
            "SELECT count(*) FROM search_candidates WHERE run_id=%s", (run_id,)
        )
        row0 = cursor.fetchone()
        assert row0 is not None
        assert row0[0] == 0
        cursor.execute(
            "SELECT count(*) FROM candidate_rankings WHERE run_id=%s", (run_id,)
        )
        row1 = cursor.fetchone()
        assert row1 is not None
        assert row1[0] == 0


@INTEGRATION
def test_unsuccessful_search_response_remains_reconstructable(tmp_path):
    migrate(TEST_DSN)
    blob_store = ContentAddressedBlobStore(tmp_path / "blobs")
    payload = json.dumps(
        {"success": False, "error": "fixture provider failure"}
    ).encode()

    with PostgresUnitOfWork(TEST_DSN, "issue-258-test-index") as uow:
        run_id = _start_run(uow, "failed-response")
        response = uow.search_responses.record_search_response(
            run_id,
            "issue 258 failed search",
            "firecrawl",
            payload,
            f"issue-258-failure-{uuid4()}",
            blob_store,
            http_status=503,
            error_message="fixture provider failure",
            transport_metadata={"attempts": 2, "exit_code": 1},
        )
        response_id = UUID(str(response["id"]))
        assert response["status"] == "provider_error"
        assert response["result_count"] == 0

    with PostgresUnitOfWork(TEST_DSN, "issue-258-test-index") as uow:
        reconstructed = uow.search_responses.get_search_response(
            response_id, run_id=run_id
        )
        assert reconstructed["status"] == "provider_error"
        assert reconstructed["error_message"] == "fixture provider failure"
        assert reconstructed["transport_metadata"]["attempts"] == 2
        assert uow.candidates.list_candidates(run_id) == []
        with uow.search_responses.open_raw_search_response_blob(
            response_id, blob_store, run_id=run_id
        ) as handle:
            assert handle.read() == payload
