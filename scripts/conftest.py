"""Session-scoped test database setup for integration tests (B-3 fix).

Applies the Alembic migration to the disposable test database before any
integration test runs.  Integration tests are skipped automatically when
RESEARCH_STORE_TEST_DATABASE_URL is not set — this fixture is a no-op in
that case.
"""

from __future__ import annotations

import os
import sys
import urllib.parse
from pathlib import Path

import pytest

def _get_test_dsn():
    try:
        with open("/opt/containers/research-postgres/secrets/research_postgres_password.txt") as f:
            pwd = urllib.parse.quote(f.read().strip(), safe="")
            return f"postgresql://research_app:{pwd}@127.0.0.1:55432/firecrawl_test"
    except FileNotFoundError:
        return "postgresql://postgres:postgres@localhost:55432/firecrawl_test"

# Authoritatively set default disposable integration test services so they aren't skipped
os.environ.setdefault("RESEARCH_STORE_TEST_DATABASE_URL", _get_test_dsn())
os.environ.setdefault("FIRECRAWL_LLM_LOCAL_BASE_URL", "http://localhost:8002/v1")
os.environ.setdefault("FIRECRAWL_LLM_LOCAL_MODEL", "chat")
os.environ.setdefault("FIRECRAWL_LLM_LOCAL_API_KEY", "EMPTY")
os.environ.setdefault("FIRECRAWL_AUDIT_LOCAL_BASE_URL", "http://localhost:8002/v1")
os.environ.setdefault("FIRECRAWL_AUDIT_LOCAL_MODEL", "chat")
os.environ.setdefault("FIRECRAWL_AUDIT_LOCAL_API_KEY", "EMPTY")


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
