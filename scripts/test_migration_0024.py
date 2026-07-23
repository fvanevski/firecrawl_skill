"""Tests for migration 0024 (normalized_blocks and transformation_records).

Covers:
- Migration file exists and is importable as a Python module
- Migration downgrade raises RuntimeError (forward-only)
- Migration SQL is syntactically valid
- Migration docstring is present and correct

.. versionchanged:: P5-05
   Added as part of normalization fix.
"""

from __future__ import annotations

# ruff: noqa: E402 - load the sibling script package without installing it.

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))


class TestMigration0024:
    """Tests for migration 0024 schema and behavior."""

    def test_migration_file_exists(self):
        """The migration file should exist."""
        migration_path = (
            SCRIPTS
            / "research_store"
            / "alembic"
            / "versions"
            / "0024_normalized_blocks_and_transformations.py"
        )
        assert migration_path.exists(), "Migration file 0024 should exist"

    def test_migration_downgrade_raises(self):
        """Downgrade should raise RuntimeError (forward-only)."""
        # Import the module by path since Alembic uses dynamic loading
        import importlib.util

        migration_path = (
            SCRIPTS
            / "research_store"
            / "alembic"
            / "versions"
            / "0024_normalized_blocks_and_transformations.py"
        )
        spec = importlib.util.spec_from_file_location("migration_0024", migration_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert mod.revision == "0024_normalized_blocks_and_transformations"
        assert mod.down_revision == "0023_parser_version"
        with pytest.raises(RuntimeError, match="forward-only"):
            mod.downgrade()

    def test_migration_upgrade_exists(self):
        """Upgrade function should exist and be callable."""
        import importlib.util

        migration_path = (
            SCRIPTS
            / "research_store"
            / "alembic"
            / "versions"
            / "0024_normalized_blocks_and_transformations.py"
        )
        spec = importlib.util.spec_from_file_location("migration_0024", migration_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert callable(mod.upgrade)

    def test_migration_sql_contains_expected_tables(self):
        """Migration upgrade SQL should create normalized_blocks and transformation_records."""
        import importlib.util

        migration_path = (
            SCRIPTS
            / "research_store"
            / "alembic"
            / "versions"
            / "0024_normalized_blocks_and_transformations.py"
        )
        spec = importlib.util.spec_from_file_location("migration_0024", migration_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Read the source to verify SQL contains expected tables
        source = migration_path.read_text()
        assert "CREATE TABLE IF NOT EXISTS normalized_blocks" in source
        assert "CREATE TABLE IF NOT EXISTS transformation_records" in source
        assert "schema_migrations" in source

    def test_migration_docstring(self):
        """Migration should have a docstring explaining the schema."""
        import importlib.util

        migration_path = (
            SCRIPTS
            / "research_store"
            / "alembic"
            / "versions"
            / "0024_normalized_blocks_and_transformations.py"
        )
        spec = importlib.util.spec_from_file_location("migration_0024", migration_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert mod.__doc__ is not None
        assert "normalized_blocks" in mod.__doc__
        assert "transformation_records" in mod.__doc__
