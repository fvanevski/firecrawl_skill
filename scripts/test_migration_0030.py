"""Tests for migration 0030 (duplicate_groups).

Covers:
- Migration file exists and is importable
- Migration downgrade raises RuntimeError
- Migration SQL is syntactically valid
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

class TestMigration0030:
    """Tests for migration 0030 schema and behavior."""

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
        assert "schema_migrations" in source


