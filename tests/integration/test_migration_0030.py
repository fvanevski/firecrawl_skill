"""Tests for migration 0030 (duplicate_groups).

Covers:
- Migration file exists and is importable
- Migration downgrade raises RuntimeError
- Migration SQL is syntactically valid
- Migration executes against a disposable PostgreSQL
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
INTEGRATION_MARK = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


def _importlib_available(name: str) -> bool:
    """Check whether a module is importable without side-effects."""
    try:
        import importlib

        importlib.import_module(name)
        return True
    except ImportError:
        return False


# Migration import tests require alembic (the migration module imports from alembic).
# These tests are skipped when alembic is not available (e.g. CI without
# requirements-research-store.txt).
_ALEMBIC_MARK = pytest.mark.skipif(
    not _importlib_available("alembic"),
    reason="alembic not installed; migration import tests require it",
)


class TestMigration0030:
    """Tests for migration 0030 schema and behavior."""

    @_ALEMBIC_MARK
    def test_migration_file_exists(self):
        """The migration file should exist."""
        migration_path = (
            SCRIPTS
            / "research_store"
            / "alembic"
            / "versions"
            / "0030_duplicate_groups.py"
        )
        assert migration_path.exists(), "Migration file 0030 should exist"

    @_ALEMBIC_MARK
    def test_migration_downgrade_raises(self):
        """Downgrade should raise RuntimeError (forward-only)."""
        import importlib.util

        migration_path = (
            SCRIPTS
            / "research_store"
            / "alembic"
            / "versions"
            / "0030_duplicate_groups.py"
        )
        spec = importlib.util.spec_from_file_location("migration_0030", migration_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert mod.revision == "0030_duplicate_groups"
        assert mod.down_revision == "0029_evidence_packets"
        with pytest.raises(RuntimeError, match="forward-only"):
            mod.downgrade()

    @_ALEMBIC_MARK
    def test_migration_sql_contains_expected_tables(self):
        """Migration upgrade SQL should create duplicate_groups."""
        import importlib.util

        migration_path = (
            SCRIPTS
            / "research_store"
            / "alembic"
            / "versions"
            / "0030_duplicate_groups.py"
        )
        spec = importlib.util.spec_from_file_location("migration_0030", migration_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        source = migration_path.read_text()
        assert "CREATE TABLE IF NOT EXISTS duplicate_groups" in source
        assert "ADD COLUMN independence_assessment" in source
        assert "schema_migrations" not in source


@INTEGRATION_MARK
def test_migration_0030_executes_against_postgresql():
    """Migration 0030 executes successfully against a disposable PostgreSQL."""
    from research_store.postgres import connect

    # The conftest already migrates to head, so 0030 should be applied.
    # Verify the tables exist.
    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT tablename FROM pg_tables
            WHERE schemaname='public'
            AND tablename = 'duplicate_groups'"""
        )
        assert cur.fetchone() is not None, "duplicate_groups table should exist"

        # Verify independence_assessment column exists on search_candidates
        cur.execute(
            """SELECT column_name FROM information_schema.columns
            WHERE table_name='search_candidates'
            AND column_name='independence_assessment'"""
        )
        assert cur.fetchone() is not None, "independence_assessment column should exist"

        # Verify the unique constraint (id, run_id) exists on duplicate_groups
        cur.execute(
            """SELECT conname FROM pg_constraint
            WHERE conrelid = 'duplicate_groups'::regclass
            AND conname = 'uk_duplicate_groups_run'"""
        )
        assert cur.fetchone() is not None, (
            "uk_duplicate_groups_run constraint should exist"
        )

        # Verify the composite FK exists
        cur.execute(
            """SELECT conname FROM pg_constraint
            WHERE conrelid = 'search_candidates'::regclass
            AND conname = 'fk_search_candidates_duplicate_group'"""
        )
        row = cur.fetchone()
        assert row is not None, "fk_search_candidates_duplicate_group FK should exist"


@INTEGRATION_MARK
def test_migration_0030_duplicate_groups_table_structure():
    """Verify duplicate_groups table has expected columns and constraints."""
    from research_store.postgres import connect

    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        # Check columns
        cur.execute(
            """SELECT column_name, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_name='duplicate_groups'
            ORDER BY ordinal_position"""
        )
        columns = {
            row[0]: {"nullable": row[1] == "YES", "default": row[2]}
            for row in cur.fetchall()
        }

        assert "id" in columns
        assert "run_id" in columns
        assert "rationale" in columns
        assert "created_at" in columns

        # id should have a default (gen_random_uuid)
        assert columns["id"]["default"] is not None
        # rationale should NOT be nullable
        assert columns["rationale"]["nullable"] is False


@INTEGRATION_MARK
def test_migration_0030_insert_and_query_duplicate_groups():
    """Verify we can insert and query duplicate_groups rows."""
    import uuid as uuid_mod
    from datetime import datetime, timezone

    from research_store.postgres import connect

    group_id = uuid_mod.uuid4()
    run_id = uuid_mod.uuid4()

    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        # Create a parent research_runs row so the FK constraint is satisfied
        cur.execute(
            """INSERT INTO research_runs (id, objective, state, execution_mode, metadata)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING""",
            (
                str(run_id),
                "test objective",
                "created",
                "agent_led",
                "{}",
            ),
        )

        # Insert a duplicate_groups row
        cur.execute(
            """INSERT INTO duplicate_groups (id, run_id, rationale, created_at)
            VALUES (%s, %s, %s, %s)
            RETURNING id, run_id, rationale""",
            (
                str(group_id),
                str(run_id),
                "test_rationale",
                datetime.now(timezone.utc),
            ),
        )
        row = cur.fetchone()
        assert row is not None
        # row[0] and row[1] are already UUIDs from PostgreSQL
        assert row[0] == group_id
        assert row[1] == run_id
        assert row[2] == "test_rationale"

        # Query it back
        cur.execute(
            """SELECT id, run_id, rationale FROM duplicate_groups
            WHERE id = %s""",
            (str(group_id),),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == group_id
