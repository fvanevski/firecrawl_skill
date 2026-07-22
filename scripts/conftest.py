"""Pytest configuration and fixtures for database storage integration testing."""

import os
from uuid import uuid4
import pytest


def pytest_addoption(parser):
    """Add command line options for PostgreSQL DSN database testing."""
    parser.addoption(
        "--pg-dsn",
        "--test-dsn",
        action="store",
        default=None,
        help=(
            "Optional PostgreSQL DSN for database storage integration testing "
            "(e.g. postgresql://postgres:postgres@localhost:5432/test_research_store)"
        ),
    )


def pytest_configure(config):
    """Configure environment for database integration testing from CLI options."""
    pg_dsn = config.getoption("pg_dsn", default=None)
    if pg_dsn:
        os.environ["RESEARCH_STORE_TEST_DATABASE_URL"] = pg_dsn
        if "RESEARCH_STORE_TEST_ALLOW_RESET" not in os.environ:
            os.environ["RESEARCH_STORE_TEST_ALLOW_RESET"] = "1"


def ensure_run_exists(database_url: str, run_id) -> None:
    """Ensure a parent research_runs row exists in PostgreSQL for foreign key constraints."""
    if not database_url or not run_id:
        return
    from research_store.postgres import connect
    with connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO research_runs (id, original_request, query_plan, skill_version, llm_model, status, state, execution_mode, objective)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING""",
            (str(run_id), "test request", "{}", "1.0", "test", "running", "created", "agent_led", "test request"),
        )


def ensure_passage_and_snapshot_exist(database_url: str, passage_id=None, snapshot_id=None) -> None:
    """Ensure parent passage (chunk) and asset_snapshot rows exist in PostgreSQL for validation."""
    if not database_url:
        return
    from research_store.postgres import connect
    with connect(database_url) as conn, conn.cursor() as cur:
        if snapshot_id or passage_id:
            source_id = uuid4()
            cur.execute(
                """INSERT INTO sources (id, canonical_url, registered_domain, metadata)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING""",
                (str(source_id), f"http://example.com/{source_id}", "example.com", "{}"),
            )
        if snapshot_id:
            cur.execute(
                """INSERT INTO asset_snapshots (id, source_id, requested_url, final_url, retrieved_at, http_status, mime_type, content_sha256, raw_blob_uri, raw_byte_length, firecrawl_version)
                VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING""",
                (str(snapshot_id), str(source_id), "http://example.com", "http://example.com", 200, "text/html", "sha256_dummy", "file:///dummy", 100, "v1"),
            )
        if passage_id:
            doc_id = uuid4()
            cur.execute(
                """INSERT INTO documents (id, snapshot_id, title, normalized_markdown, normalized_text, parser_name, parser_version, normalization_version, document_sha256)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING""",
                (str(doc_id), str(snapshot_id) if snapshot_id else None, "test doc", "markdown", "text", "test", "1.0", "1.0", "sha256_dummy_doc"),
            )
            cur.execute(
                """INSERT INTO chunks (id, document_id, ordinal, text, token_count, content_sha256, chunker_name, chunker_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING""",
                (str(passage_id), str(doc_id), 0, "sample text", 2, "sha256_dummy", "test", "1.0"),
            )


@pytest.fixture(scope="session")
def test_pg_dsn():
    """Session fixture returning the optional PostgreSQL test DSN if configured."""
    return os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def prepared_database(test_pg_dsn):
    """Session fixture to reset and migrate disposable test PostgreSQL database."""
    if not test_pg_dsn:
        return
    from research_store.postgres import connect, migrate, require_disposable_database_reset
    require_disposable_database_reset(test_pg_dsn, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", ""))
    with connect(test_pg_dsn) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")
    migrate(test_pg_dsn)
