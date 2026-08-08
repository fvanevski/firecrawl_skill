"""Session-scoped test database setup for integration tests (B-3 fix).

Applies the Alembic migration to the disposable test database before any
integration test runs.  Integration tests are skipped automatically when
RESEARCH_STORE_TEST_DATABASE_URL is not set — this fixture is a no-op in
that case.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Clear the append-only promotion ledger before legacy rebuild cleanup.

    ``TestIndexRebuildRecovery.setup_method`` resets shared corpus/index rows
    with dependency-ordered ``DELETE`` statements. Migration 0040 adds the
    append-only ``run_asset_promotion_events`` ledger, whose row trigger
    intentionally rejects ``DELETE`` even in tests. Truncating that test-only
    ledger before pytest invokes the class setup preserves the production
    append-only invariant while allowing the existing cleanup to proceed.
    """
    if (
        not TEST_DSN
        or item.cls is None
        or item.cls.__name__ != "TestIndexRebuildRecovery"
    ):
        return

    from research_store.postgres import connect

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        # Truncate append-only ledgers before pytest invokes the class setup.
        # These tables have row-level triggers that reject DELETE in production,
        # but tests need a clean slate between TestIndexRebuildRecovery test
        # methods. TRUNCATE bypasses row-level triggers while preserving the
        # production append-only invariant. Use CASCADE to handle foreign keys
        # (e.g., indexing_checkpoints references run_asset_membership_seals).
        cursor.execute("TRUNCATE TABLE run_asset_promotion_events")
        cursor.execute("TRUNCATE TABLE run_asset_membership_members")
        cursor.execute("TRUNCATE TABLE indexing_checkpoint_observations")
        cursor.execute("TRUNCATE TABLE run_asset_membership_seals CASCADE")


@pytest.fixture(scope="session", autouse=True)
def _apply_db_schema():
    """Apply Alembic migrations to the test database once per session.

    Skipped when RESEARCH_STORE_TEST_DATABASE_URL is not set so that unit
    tests continue to run without a database.

    The migration target is "head" so that every integration test run
    always executes against the latest schema.  The database must already
    exist — only schema objects (tables, indexes, extensions) are created,
    never the database itself.
    """
    if not TEST_DSN:
        return  # No DB available; integration tests will self-skip via _integration()

    from research_store.postgres import migrate

    try:
        migrate(TEST_DSN)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"Failed to apply Alembic migrations to test database: {exc}\n"
            f"DSN: {TEST_DSN[:40]}..."
        )


# ---------------------------------------------------------------------------
# Shared database fixtures and helpers for integration tests
# ---------------------------------------------------------------------------


def ensure_run_exists(dsn, run_id):
    """Insert a test research_run if it does not already exist."""
    from research_store.postgres import connect

    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO research_runs (id, objective, query_plan, skill_version,
            llm_model, state, execution_mode)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
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
        conn.commit()


def ensure_passage_and_snapshot_exist(dsn, passage_id, snapshot_id):
    """Create source → snapshot → document → chunk chain for evidence-link tests."""
    from research_store.postgres import connect

    document_id = uuid4()
    source_id = uuid4()

    with connect(dsn) as conn, conn.cursor() as cur:
        # 1. Create a source record (sources table)
        cur.execute(
            """INSERT INTO sources (id, canonical_url, source_type, registered_domain)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (canonical_url) DO NOTHING""",
            (
                str(source_id),
                f"https://example.com/doc/{document_id}",
                "web",
                "example.com",
            ),
        )

        # 2. Create an asset snapshot (asset_snapshots table)
        cur.execute(
            """INSERT INTO asset_snapshots
                (id, source_id, requested_url, final_url, retrieved_at,
                 content_sha256, raw_blob_uri, raw_byte_length, mime_type,
                 firecrawl_version, crawl_options)
            VALUES (%s, %s, %s, %s, now(), %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING""",
            (
                str(snapshot_id),
                str(source_id),
                f"https://example.com/doc/{document_id}",
                f"https://example.com/doc/{document_id}",
                hashlib.sha256(b"").hexdigest(),
                "blob://dummy",
                0,
                "text/plain",
                "0.0.0",
                "{}",
            ),
        )

        # 3. Create a document (documents table) referencing the snapshot.
        #    No ON CONFLICT clause — if the snapshot_id does not exist, the
        #    FK constraint will raise, making test setup failures visible.
        cur.execute(
            """INSERT INTO documents
                (id, snapshot_id, title, parser_name, parser_version,
                 normalization_version, document_sha256, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                str(document_id),
                str(snapshot_id),
                "Test Document",
                "markdown-v1",
                "1.0",
                "cleanup-v1",
                hashlib.sha256(b"").hexdigest(),
                "{}",
            ),
        )

        # 4. Create a chunk (chunks table) referencing the document
        cur.execute(
            """INSERT INTO chunks (id, document_id, ordinal, text, content_sha256, token_count, chunker_name, chunker_version, tokenizer_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (
                str(passage_id),
                str(document_id),
                0,
                "test text",
                hashlib.sha256(b"test text").hexdigest(),
                2,
                "structural-v1",
                "1.0",
                "cl100k_base",
            ),
        )
        conn.commit()


@pytest.fixture(scope="session")
def prepared_database_for_claims():
    """Reset and migrate the test database to the current Alembic head.

    Drops and recreates the public schema so every integration test starts
    from a clean slate.  Migrates to the current head (not just 0017) so
    that all tables required by downstream fixtures are present.
    """
    from research_store.postgres import (
        connect,
        migrate,
        require_disposable_database_reset,
    )

    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
    # Migrates to the current Alembic head (0036_run_performance_telemetry).
    # The assertion confirms the migration returned a non-zero revision count.
    assert migrate(TEST_DSN) >= 1


@pytest.fixture(scope="session")
def prepared_database_for_evidence_packets(prepared_database_for_claims):
    """No-op fixture — ``prepared_database_for_claims`` already migrates to head."""
