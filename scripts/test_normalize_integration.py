"""Opt-in integration tests for ``research-db normalize`` CLI command.

Set RESEARCH_STORE_TEST_DATABASE_URL to a disposable PostgreSQL database whose
name contains a standalone ``test`` segment, and set
RESEARCH_STORE_TEST_ALLOW_RESET to that exact database name.

.. versionchanged:: P5-05
   Added as part of normalization fix.
"""

from __future__ import annotations

# ruff: noqa: E402 - load the sibling script package without installing it.

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.config import StoreConfig
from research_store.postgres import connect, migrate, require_disposable_database_reset

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


@pytest.fixture(scope="session")
def normalize_database():
    """Prepare a disposable database with migrations through v24."""
    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    migrated = migrate(TEST_DSN)
    assert migrated >= 24, f"Expected at least 24 migrations, got {migrated}"


@pytest.fixture
def service(normalize_database):
    """Build a CorpusService for the test database."""
    from research_store.container import build_service

    config = StoreConfig.from_env()
    config = config.replace(database_url=TEST_DSN)
    return build_service(config)


class TestNormalizeCLI:
    """Integration tests for ``research-db normalize``."""

    def test_normalize_no_documents(self):
        """When no documents exist, normalize should return 0 blocks."""
        from research_store.cli import parser

        args = parser().parse_args(["normalize", "--all"])
        from research_store.cli import _cmd_normalize

        from research_store.config import StoreConfig

        config = StoreConfig.from_env().replace(database_url=TEST_DSN)
        rc = _cmd_normalize(config, args)
        assert rc == 0

    def test_normalize_with_blocks(self, service):
        """Normalizing a document with blocks should persist normalized blocks."""
        from research_store.cli import parser
        from research_store.cli import _cmd_normalize
        from research_store.config import StoreConfig
        from research_store.domain import IngestRequest

        # Ingest a simple document
        request = IngestRequest(
            requested_url="https://example.com/test-normalize",
            content=b"Hello world\n\nThis is a test document.\n\nUse cookies to improve.\n\n```python\nprint('hello')\n```",
            mime_type="text/markdown",
            title="Test Normalize Document",
        )

        result = service.ingest(request)
        doc_uuid = result.document_id

        # Run normalize
        args = parser().parse_args(["normalize", "--document", str(doc_uuid)])
        config = StoreConfig.from_env().replace(database_url=TEST_DSN)
        rc = _cmd_normalize(config, args)
        assert rc == 0

        # Verify normalized blocks were persisted
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM normalized_blocks WHERE document_id = %s",
                (str(doc_uuid),),
            )
            count = cur.fetchone()[0]
            assert count > 0, "Expected normalized blocks to be persisted"

        # Verify transformation records were persisted
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM transformation_records tr
                   JOIN normalized_blocks nb ON nb.source_block_id = tr.normalized_block_id
                   WHERE nb.document_id = %s""",
                (str(doc_uuid),),
            )
            count = cur.fetchone()[0]
            assert count > 0, "Expected transformation records to be persisted"

    def test_normalize_idempotent(self, service):
        """Re-running normalize should upsert without errors."""
        from research_store.cli import parser
        from research_store.cli import _cmd_normalize
        from research_store.config import StoreConfig
        from research_store.domain import IngestRequest

        # Ingest a document
        request = IngestRequest(
            requested_url="https://example.com/test-normalize-idem",
            content=b"Hello world\n\nUse cookies to improve.",
            mime_type="text/markdown",
            title="Test Idempotent Normalize",
        )
        result = service.ingest(request)
        doc_uuid = result.document_id

        # Run normalize twice
        args = parser().parse_args(["normalize", "--document", str(doc_uuid)])
        config = StoreConfig.from_env().replace(database_url=TEST_DSN)

        rc1 = _cmd_normalize(config, args)
        rc2 = _cmd_normalize(config, args)
        assert rc1 == 0
        assert rc2 == 0

        # Verify counts haven't doubled
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM normalized_blocks WHERE document_id = %s",
                (str(doc_uuid),),
            )
            count = cur.fetchone()[0]
            # Should be exactly one row per source block, not double
            assert count > 0

    def test_normalize_missing_document(self):
        """Normalizing a non-existent document should succeed with 0 blocks."""
        from research_store.cli import parser
        from research_store.cli import _cmd_normalize
        from research_store.config import StoreConfig

        fake_uuid = uuid4()
        args = parser().parse_args(["normalize", "--document", str(fake_uuid)])
        config = StoreConfig.from_env().replace(database_url=TEST_DSN)
        rc = _cmd_normalize(config, args)
        assert rc == 0

    def test_normalize_without_args_fails(self):
        """Normalize without --document or --all should return error."""
        from research_store.cli import parser
        from research_store.cli import _cmd_normalize
        from research_store.config import StoreConfig

        args = parser().parse_args(["normalize"])
        config = StoreConfig.from_env().replace(database_url=TEST_DSN)
        rc = _cmd_normalize(config, args)
        assert rc == 1
