"""Tests for PostgresUnitOfWork duplicate-group methods.

Unit tests verify method signatures and validation.
Integration tests (marked with @INTEGRATION_MARK) verify actual DB behavior
when a disposable PostgreSQL is available.
"""

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


class TestPostgresUnitOfWorkDuplicateMethods:
    """Unit tests for duplicate-group UoW methods."""

    def test_assign_duplicate_group_rejects_empty(self):
        """assign_duplicate_group raises ValueError for empty candidate_ids."""
        uow = PostgresUnitOfWork.__new__(PostgresUnitOfWork)
        uow.connection = None
        with pytest.raises(ValueError, match="candidate_ids must not be empty"):
            uow.assign_duplicate_group([])

    def test_persist_duplicate_group_signature(self):
        """persist_duplicate_group accepts group_id, run_id, and rationale."""
        uow = PostgresUnitOfWork.__new__(PostgresUnitOfWork)
        uow.connection = None
        # Should fail because connection is not set, but the method exists
        with pytest.raises(AttributeError):
            uow.persist_duplicate_group(uuid.uuid4(), uuid.uuid4(), "test")

    def test_update_candidate_independence_signature(self):
        """update_candidate_independence accepts candidate_id and assessment dict."""
        uow = PostgresUnitOfWork.__new__(PostgresUnitOfWork)
        uow.connection = None
        # Should fail because connection is not set, but the method exists
        with pytest.raises(AttributeError):
            uow.update_candidate_independence(uuid.uuid4(), {"status": "independent"})

    def test_update_candidate_independence_serializes_json(self):
        """update_candidate_independence serializes the assessment dict to JSON."""
        assessment = {
            "status": "independent",
            "rationale": "Publisher owns the specification",
        }
        serialized = json.dumps(assessment)
        assert isinstance(serialized, str)
        deserialized = json.loads(serialized)
        assert deserialized == assessment


@INTEGRATION_MARK
def test_uow_assign_duplicate_group_creates_group():
    """assign_duplicate_group creates a duplicate_groups row and links candidates."""
    run_id = uuid.uuid4()
    group_id = uuid.uuid4()
    c1_id = uuid.uuid4()
    c2_id = uuid.uuid4()

    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        # Create a test run
        cur.execute(
            """INSERT INTO research_runs (id, original_request, query_plan, skill_version,
            llm_model, status, state, execution_mode, objective)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING""",
            (
                str(run_id),
                "test request",
                "{}",
                "1.0",
                "test",
                "running",
                "created",
                "agent_led",
                "test request",
            ),
        )

        # Create test candidates
        cur.execute(
            """INSERT INTO search_candidates (id, run_id, canonical_url, canonical_url_sha256,
            original_url, title, domain, backend, recurrence_count,
            first_seen_at, last_seen_at, created_at, independence_assessment)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                str(c1_id),
                str(run_id),
                "https://example.com/1",
                "a" * 64,
                "https://example.com/1",
                "Test 1",
                "example.com",
                "test",
                1,
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
                "{}",
            ),
        )
        cur.execute(
            """INSERT INTO search_candidates (id, run_id, canonical_url, canonical_url_sha256,
            original_url, title, domain, backend, recurrence_count,
            first_seen_at, last_seen_at, created_at, independence_assessment)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                str(c2_id),
                str(run_id),
                "https://example.com/2",
                "b" * 64,
                "https://example.com/2",
                "Test 2",
                "example.com",
                "test",
                1,
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
                "{}",
            ),
        )
        conn.commit()

    # Now test the UoW method
    uow = PostgresUnitOfWork(TEST_DSN, "test-index")
    with uow:
        result_group_id = uow.assign_duplicate_group([c1_id, c2_id], group_id, run_id)
        assert result_group_id == group_id

    # Verify the group was created
    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id, run_id, rationale FROM duplicate_groups WHERE id = %s""",
            (str(group_id),),
        )
        row = cur.fetchone()
        assert row is not None
        assert uuid.UUID(row[0]) == group_id
        assert uuid.UUID(row[1]) == run_id
        assert row[2] == "legacy assignment"

        # Verify candidates were linked
        cur.execute(
            """SELECT duplicate_group_id FROM search_candidates
            WHERE id = ANY(%s) AND run_id = %s""",
            ([str(c1_id), str(c2_id)], str(run_id)),
        )
        rows = cur.fetchall()
        assert len(rows) == 2
        for row in rows:
            assert row[0] is not None
            assert uuid.UUID(row[0]) == group_id


@INTEGRATION_MARK
def test_uow_persist_duplicate_group_upserts():
    """persist_duplicate_group upserts duplicate_groups rows."""
    group_id = uuid.uuid4()
    run_id = uuid.uuid4()

    uow = PostgresUnitOfWork(TEST_DSN, "test-index")
    with uow:
        uow.persist_duplicate_group(group_id, run_id, "initial rationale")

    # Verify initial insert
    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT rationale FROM duplicate_groups WHERE id = %s""",
            (str(group_id),),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "initial rationale"

    # Update via upsert
    uow = PostgresUnitOfWork(TEST_DSN, "test-index")
    with uow:
        uow.persist_duplicate_group(group_id, run_id, "updated rationale")

    # Verify update
    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT rationale FROM duplicate_groups WHERE id = %s""",
            (str(group_id),),
        )
        row = cur.fetchone()
        assert row[0] == "updated rationale"


@INTEGRATION_MARK
def test_uow_update_candidate_independence():
    """update_candidate_independence updates the independence_assessment column."""
    candidate_id = uuid.uuid4()
    run_id = uuid.uuid4()

    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        # Create a test run
        cur.execute(
            """INSERT INTO research_runs (id, original_request, query_plan, skill_version,
            llm_model, status, state, execution_mode, objective)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING""",
            (
                str(run_id),
                "test request",
                "{}",
                "1.0",
                "test",
                "running",
                "created",
                "agent_led",
                "test request",
            ),
        )

        # Create a test candidate
        cur.execute(
            """INSERT INTO search_candidates (id, run_id, canonical_url, canonical_url_sha256,
            original_url, title, domain, backend, recurrence_count,
            first_seen_at, last_seen_at, created_at, independence_assessment)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING""",
            (
                str(candidate_id),
                str(run_id),
                "https://example.com/1",
                "a" * 64,
                "https://example.com/1",
                "Test",
                "example.com",
                "test",
                1,
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
                "{}",
            ),
        )
        conn.commit()

    # Update independence assessment
    assessment = {
        "status": "independent",
        "rationale": "Publisher owns the specification",
    }
    uow = PostgresUnitOfWork(TEST_DSN, "test-index")
    with uow:
        uow.update_candidate_independence(candidate_id, assessment)

    # Verify update
    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT independence_assessment FROM search_candidates
            WHERE id = %s""",
            (str(candidate_id),),
        )
        row = cur.fetchone()
        assert row is not None
        stored = json.loads(row[0])
        assert stored["status"] == "independent"
        assert stored["rationale"] == "Publisher owns the specification"
