"""Session-scoped test database setup for integration tests (B-3 fix).

Applies the Alembic migration to the disposable test database before any
integration test runs.  Integration tests are skipped automatically when
RESEARCH_STORE_TEST_DATABASE_URL is not set — this fixture is a no-op in
that case.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")


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
