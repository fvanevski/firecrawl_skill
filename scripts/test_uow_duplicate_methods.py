"""Positive repository regressions for candidate duplicate-group persistence."""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.postgres import PostgresUnitOfWork, connect

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
INTEGRATION_MARK = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


class _FakeConnection:
    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def transaction(self):
        raise AssertionError("candidate validation must not create a savepoint")


class TestCandidateDuplicateRepository:
    def test_duplicate_operations_have_one_repository_owner(self, monkeypatch):
        fake_connection = _FakeConnection()
        monkeypatch.setattr(
            "research_store.postgres.connect", lambda _database_url: fake_connection
        )

        for name in (
            "assign_duplicate_group",
            "persist_duplicate_group",
            "update_candidate_independence",
        ):
            assert name not in PostgresUnitOfWork.__dict__

        with PostgresUnitOfWork("postgresql://test.invalid/db", "test-index") as uow:
            assert callable(uow.candidates.assign_duplicate_group)
            assert callable(uow.candidates.persist_duplicate_group)
            assert callable(uow.candidates.update_candidate_independence)
            assert uow.candidates.connection_identity == id(fake_connection)

    def test_assign_duplicate_group_rejects_empty_through_repository(self, monkeypatch):
        fake_connection = _FakeConnection()
        monkeypatch.setattr(
            "research_store.postgres.connect", lambda _database_url: fake_connection
        )

        with (
            PostgresUnitOfWork("postgresql://test.invalid/db", "test-index") as uow,
            pytest.raises(ValueError, match="candidate_ids must not be empty"),
        ):
            uow.candidates.assign_duplicate_group([])

    def test_independence_assessment_remains_json_serializable(self):
        assessment = {
            "status": "independent",
            "rationale": "Publisher owns the specification",
        }
        assert json.loads(json.dumps(assessment)) == assessment


def _insert_test_run(cur, run_id):
    cur.execute(
        """INSERT INTO research_runs (
            id, objective, query_plan, skill_version, llm_model, state, execution_mode
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING""",
        (
            str(run_id),
            "test request",
            "{}",
            "1.0",
            "test",
            "created",
            "agent_led",
        ),
    )


def _insert_candidate(cur, candidate_id, run_id, ordinal):
    now = datetime.now(timezone.utc)
    cur.execute(
        """INSERT INTO search_candidates (
            id, run_id, canonical_url, canonical_url_sha256,
            original_url, title, domain, backend, recurrence_count,
            first_seen_at, last_seen_at, created_at, independence_assessment
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            str(candidate_id),
            str(run_id),
            f"https://example.com/{ordinal}",
            str(ordinal) * 64,
            f"https://example.com/{ordinal}",
            f"Test {ordinal}",
            "example.com",
            "test",
            1,
            now,
            now,
            now,
            "{}",
        ),
    )


@INTEGRATION_MARK
def test_candidate_repository_assign_duplicate_group_creates_group():
    run_id = uuid.uuid4()
    group_id = uuid.uuid4()
    candidate_ids = [uuid.uuid4(), uuid.uuid4()]

    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        _insert_test_run(cur, run_id)
        for ordinal, candidate_id in enumerate(candidate_ids, 1):
            _insert_candidate(cur, candidate_id, run_id, ordinal)
        conn.commit()

    with PostgresUnitOfWork(TEST_DSN, "test-index") as uow:
        assert (
            uow.candidates.assign_duplicate_group(candidate_ids, group_id, run_id)
            == group_id
        )

    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, run_id, rationale FROM duplicate_groups WHERE id = %s",
            (str(group_id),),
        )
        row = cur.fetchone()
        assert row is not None
        db_group_id = uuid.UUID(row[0]) if isinstance(row[0], str) else row[0]
        db_run_id = uuid.UUID(row[1]) if isinstance(row[1], str) else row[1]
        assert db_group_id == group_id
        assert db_run_id == run_id
        assert row[2] == "legacy assignment"

        cur.execute(
            """SELECT duplicate_group_id FROM search_candidates
            WHERE id = ANY(%s) AND run_id = %s""",
            ([str(value) for value in candidate_ids], str(run_id)),
        )
        rows = cur.fetchall()
        assert len(rows) == 2
        assert all(
            (uuid.UUID(item[0]) if isinstance(item[0], str) else item[0]) == group_id
            for item in rows
        )


@INTEGRATION_MARK
def test_candidate_repository_persist_duplicate_group_upserts():
    group_id = uuid.uuid4()
    run_id = uuid.uuid4()

    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        _insert_test_run(cur, run_id)
        conn.commit()

    with PostgresUnitOfWork(TEST_DSN, "test-index") as uow:
        uow.candidates.persist_duplicate_group(group_id, run_id, "initial rationale")
    with PostgresUnitOfWork(TEST_DSN, "test-index") as uow:
        uow.candidates.persist_duplicate_group(group_id, run_id, "updated rationale")

    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT rationale FROM duplicate_groups WHERE id = %s", (str(group_id),)
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "updated rationale"


@INTEGRATION_MARK
def test_candidate_repository_updates_independence_assessment():
    candidate_id = uuid.uuid4()
    run_id = uuid.uuid4()

    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        _insert_test_run(cur, run_id)
        _insert_candidate(cur, candidate_id, run_id, 3)
        conn.commit()

    assessment = {
        "status": "independent",
        "rationale": "Publisher owns the specification",
    }
    with PostgresUnitOfWork(TEST_DSN, "test-index") as uow:
        uow.candidates.update_candidate_independence(candidate_id, assessment)

    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT independence_assessment FROM search_candidates WHERE id = %s",
            (str(candidate_id),),
        )
        row = cur.fetchone()
        assert row is not None
        stored = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        assert stored == assessment
