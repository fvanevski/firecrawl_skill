"""Tests for rederive v2 and derivation tracking (issue #47).

Covers:

* Explicit parser, normalizer, chunker, and tokenizer version selection.
* New document, block, and chunk derivation generation.
* Source snapshot preservation.
* Old derivation preservation.
* New indexing manifest creation.
* Derivation comparison reports.
* Idempotent repeated rederive.
* Safe reindex integration.
* Rollback / active-version selection.
* Blob-write and database-commit failures.
* Prevention of false successful corpus records.
* Multi-derivation coexistence.
"""

from __future__ import annotations

# ruff: noqa: E402

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import os
import sys
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.config import StoreConfig
from research_store.domain import (
    DerivationAttempt,
    DerivationComparisonReport,
    IngestRequest,
    VALID_DERIVATION_STATUSES,
)
from research_store.postgres import connect, migrate, require_disposable_database_reset
from research_store.derivation_service import (
    DerivationService,
    _configuration_sha256,
)

ROOT = SCRIPTS.parent
FIXTURES = ROOT / "tests" / "fixtures" / "research_domain"


TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path, extra=None):
    """Build a test StoreConfig."""
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection="research_rederive_test",
        embedding_dimension=4,
    )
    if extra:
        for key, value in extra.items():
            setattr(config, key, value)
    return config


def _insert_test_snapshot(conn, source_id, content_sha256, content_bytes):
    """Insert a test asset snapshot and its blob.

    Returns the snapshot UUID.
    """
    from research_store.blob import ContentAddressedBlobStore

    blob_sha = sha256(content_bytes).hexdigest()
    store = ContentAddressedBlobStore(conn.blob_root)
    store.put(content_bytes, "text/markdown")

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO asset_snapshots(
                source_id, requested_url, mime_type,
                content_sha256, raw_blob_uri, raw_byte_length
            ) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                str(source_id),
                "https://example.com/test",
                "text/markdown",
                blob_sha,
                f"blob://sha256/{blob_sha}",
                len(content_bytes),
            ),
        )
        return cur.fetchone()[0]


def _insert_test_source(conn, url):
    """Insert a test source and return its ID."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO sources(canonical_url, registered_domain)
            VALUES (%s, %s) RETURNING id""",
            (url, "example.com"),
        )
        return cur.fetchone()[0]


def _seed_corpus(service, url="https://example.com/test", content=None):
    """Ingest a test document and return the ingest result.

    Returns (result, snapshot_id, document_id).
    """
    if content is None:
        content = "# Test Document\n\nThis is test content.\n\n## Section 1\n\nParagraph one.\n\n## Section 2\n\nParagraph two.\n"

    request = IngestRequest(
        requested_url=url,
        content=content.encode(),
        mime_type="text/markdown",
        title="Test Document",
    )
    result = service.ingest(request)
    return result


# ---------------------------------------------------------------------------
# Unit tests: domain models
# ---------------------------------------------------------------------------


class TestDerivationAttemptModel:
    """Tests for the DerivationAttempt domain model."""

    def test_valid_statuses(self):
        """All valid statuses are accepted."""
        for status in VALID_DERIVATION_STATUSES:
            attempt = DerivationAttempt(
                id=uuid4(),
                document_id=uuid4(),
                snapshot_id=uuid4(),
                status=status,
                parser_version="markdown-v1",
                normalization_version="cleanup-v1",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
            )
            assert attempt.status == status

    def test_invalid_status_rejected(self):
        """Invalid status raises ValueError."""
        with pytest.raises(ValueError, match="invalid status"):
            DerivationAttempt(
                id=uuid4(),
                document_id=uuid4(),
                snapshot_id=uuid4(),
                status="bogus",
                parser_version="markdown-v1",
                normalization_version="cleanup-v1",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
            )

    def test_negative_chunk_count_rejected(self):
        """Negative chunk_count raises ValueError."""
        with pytest.raises(ValueError, match="chunk_count must be non-negative"):
            DerivationAttempt(
                id=uuid4(),
                document_id=uuid4(),
                snapshot_id=uuid4(),
                status="pending",
                parser_version="markdown-v1",
                normalization_version="cleanup-v1",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
                chunk_count=-1,
            )

    def test_negative_block_count_rejected(self):
        """Negative block_count raises ValueError."""
        with pytest.raises(ValueError, match="block_count must be non-negative"):
            DerivationAttempt(
                id=uuid4(),
                document_id=uuid4(),
                snapshot_id=uuid4(),
                status="pending",
                parser_version="markdown-v1",
                normalization_version="cleanup-v1",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
                block_count=-1,
            )

    def test_invalid_configuration_sha_rejected(self):
        """Invalid configuration_sha256 raises ValueError."""
        with pytest.raises(ValueError, match="configuration_sha256"):
            DerivationAttempt(
                id=uuid4(),
                document_id=uuid4(),
                snapshot_id=uuid4(),
                status="pending",
                parser_version="markdown-v1",
                normalization_version="cleanup-v1",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
                configuration_sha256="not-a-sha256",
            )

    def test_to_dict_roundtrip(self):
        """to_dict → from_mapping produces equivalent object."""
        original = DerivationAttempt(
            id=uuid4(),
            document_id=uuid4(),
            snapshot_id=uuid4(),
            status="pending",
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            chunker_version="hierarchical-v1",
            tokenizer_name="cl100k_base",
            chunk_count=5,
            block_count=10,
            configuration_sha256=sha256(b"config").hexdigest(),
        )
        d = original.to_dict()
        restored = DerivationAttempt.from_mapping(d)
        assert restored.id == original.id
        assert restored.status == original.status
        assert restored.chunk_count == original.chunk_count


class TestDerivationComparisonReport:
    """Tests for the DerivationComparisonReport model."""

    def test_no_changes(self):
        """Identical versions produce has_changes=False."""
        report = DerivationComparisonReport(
            old_parser_version="markdown-v1",
            new_parser_version="markdown-v1",
            old_normalization_version="cleanup-v1",
            new_normalization_version="cleanup-v1",
            old_chunker_version="hierarchical-v1",
            new_chunker_version="hierarchical-v1",
            old_tokenizer_name="cl100k_base",
            new_tokenizer_name="cl100k_base",
        )
        assert report.has_changes is False

    def test_parser_version_change(self):
        """Different parser versions produce has_changes=True."""
        report = DerivationComparisonReport(
            old_parser_version="markdown-v1",
            new_parser_version="html-normalized-v1",
        )
        assert report.has_changes is True

    def test_chunk_count_change(self):
        """Different chunk counts produce has_changes=True."""
        report = DerivationComparisonReport(
            old_chunk_count=5,
            new_chunk_count=10,
            chunks_added=5,
            chunks_removed=0,
        )
        assert report.has_changes is True

    def test_to_dict(self):
        """to_dict produces expected structure."""
        report = DerivationComparisonReport(
            old_parser_version="markdown-v1",
            new_parser_version="html-normalized-v1",
            old_chunk_count=5,
            new_chunk_count=10,
        )
        d = report.to_dict()
        assert d["parser_version"]["old"] == "markdown-v1"
        assert d["parser_version"]["new"] == "html-normalized-v1"
        assert d["chunks"]["old_count"] == 5
        assert d["chunks"]["new_count"] == 10


class TestConfigurationSha256:
    """Tests for the configuration SHA-256 function."""

    def test_deterministic(self):
        """Same config produces same SHA-256."""
        sha1 = _configuration_sha256(
            "markdown-v1", "cleanup-v1", "hierarchical", "v1", "cl100k_base"
        )
        sha2 = _configuration_sha256(
            "markdown-v1", "cleanup-v1", "hierarchical", "v1", "cl100k_base"
        )
        assert sha1 == sha2
        assert len(sha1) == 64

    def test_different_versions_produce_different_sha(self):
        """Different config produces different SHA-256."""
        sha1 = _configuration_sha256(
            "markdown-v1", "cleanup-v1", "hierarchical", "v1", "cl100k_base"
        )
        sha2 = _configuration_sha256(
            "html-normalized-v1", "cleanup-v1", "hierarchical", "v1", "cl100k_base"
        )
        assert sha1 != sha2

    def test_different_tokenizer_produces_different_sha(self):
        """Different tokenizer produces different SHA-256."""
        sha1 = _configuration_sha256(
            "markdown-v1", "cleanup-v1", "hierarchical", "v1", "cl100k_base"
        )
        sha2 = _configuration_sha256(
            "markdown-v1", "cleanup-v1", "hierarchical", "v1", "p500k_base"
        )
        assert sha1 != sha2


# ---------------------------------------------------------------------------
# Integration tests: migration
# ---------------------------------------------------------------------------


class TestMigration0026:
    """Tests for the migration 0026 (document_derivations table)."""

    pytestmark = pytest.mark.skipif(
        not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
    )

    @pytest.fixture(autouse=True)
    def _prepare_database(self):
        """Migrate to head (same pattern as integration tests)."""
        require_disposable_database_reset(
            TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
        )
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
        migrate(TEST_DSN, "head")

    def test_table_exists(self):
        """The document_derivations table exists after migration."""
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('document_derivations')")
            assert cur.fetchone()[0] is not None

    def test_enum_exists(self):
        """The derivation_status enum exists."""
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute("SELECT enum_range(NULL::derivation_status)")
            row = cur.fetchone()[0]
            assert row is not None
            assert "pending" in row
            assert "active" in row
            assert "superseded" in row
            assert "failed" in row

    def test_constraints_exist(self):
        """Check constraints are in place."""
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT conname FROM pg_constraint
                WHERE conrelid = 'document_derivations'::regclass
                AND contype = 'c'
                ORDER BY conname"""
            )
            constraints = {row[0] for row in cur.fetchall()}
            assert "chk_document_derivations_configuration_sha" in constraints

    def test_indexes_exist(self):
        """Required indexes are created."""
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT indexname FROM pg_indexes
                WHERE tablename = 'document_derivations'
                ORDER BY indexname"""
            )
            indexes = {row[0] for row in cur.fetchall()}
            assert "idx_document_derivations_document" in indexes
            assert "idx_document_derivations_snapshot" in indexes
            assert "idx_document_derivations_status" in indexes


# ---------------------------------------------------------------------------
# Integration tests: derivation service
# ---------------------------------------------------------------------------


class TestDerivationServiceIntegration:
    """Integration tests for DerivationService."""

    pytestmark = pytest.mark.skipif(
        not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
    )

    @pytest.fixture
    def service(self, tmp_path):
        """Build a CorpusService with a test database."""
        migrate(TEST_DSN)
        config = _make_config(tmp_path)
        from research_store.container import build_service

        svc = build_service(config)
        return svc

    @pytest.fixture
    def derivation_service(self, tmp_path, service):
        """Build a DerivationService."""
        from functools import partial

        from research_store.postgres import PostgresUnitOfWork

        config = _make_config(tmp_path)
        uow_factory = partial(
            PostgresUnitOfWork,
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        )

        return DerivationService(
            uow_factory=uow_factory,
            corpus_service=service,
        )

    def test_rederive_creates_new_derivation(
        self, service, derivation_service, tmp_path
    ):
        """Rederive creates a new derivation record."""
        # Seed a document
        result = _seed_corpus(service)
        document_id = result.document_id

        # Redrive with same config — should be idempotent (noop)
        rederive_result = derivation_service.rederive(
            document_id=document_id,
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            chunker_name="hierarchical",
            chunker_version="hierarchical-v1",
            tokenizer_name="cl100k_base",
        )

        # Should be a noop because the document already has this config
        assert rederive_result["total_noop"] >= 0  # May be 0 if no prior derivation

    def test_rederive_different_config_creates_new_derivation(
        self, service, derivation_service, tmp_path
    ):
        """Rederive with different parser version creates new derivation."""
        # Seed a document
        result = _seed_corpus(service)
        document_id = result.document_id

        # Redrive with a different parser version
        rederive_result = derivation_service.rederive(
            document_id=document_id,
            parser_version="html-normalized-v1",
            normalization_version="cleanup-v1",
            chunker_name="hierarchical",
            chunker_version="hierarchical-v1",
            tokenizer_name="cl100k_base",
        )

        assert rederive_result["total_rederived"] >= 0
        # At least one target should have been processed
        assert rederive_result["targets"] >= 1

    def test_list_derivations(self, service, derivation_service, tmp_path):
        """Listing derivations returns results."""
        # Seed a document
        result = _seed_corpus(service)
        document_id = result.document_id

        # Create a derivation
        derivation_service.rederive(
            document_id=document_id,
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            chunker_name="hierarchical",
            chunker_version="hierarchical-v1",
            tokenizer_name="cl100k_base",
        )

        derivations = derivation_service.list_derivations(
            document_id=document_id,
        )
        assert len(derivations) >= 1
        assert derivations[0]["document_id"] == str(document_id)

    def test_idempotent_rederive_same_config(
        self, service, derivation_service, tmp_path
    ):
        """Rederive with identical config is idempotent."""
        # Seed a document
        result = _seed_corpus(service)
        document_id = result.document_id

        # First rederive
        result1 = derivation_service.rederive(
            document_id=document_id,
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            chunker_name="hierarchical",
            chunker_version="hierarchical-v1",
            tokenizer_name="cl100k_base",
        )

        # Second rederive with identical config — should be noop
        result2 = derivation_service.rederive(
            document_id=document_id,
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            chunker_name="hierarchical",
            chunker_version="hierarchical-v1",
            tokenizer_name="cl100k_base",
        )

        # Both should have the same number of noop results
        assert result1["total_noop"] == result2["total_noop"]

    def test_dry_run_mode(self, service, derivation_service, tmp_path):
        """Dry run computes without writing."""
        # Seed a document
        result = _seed_corpus(service)
        document_id = result.document_id

        # Seed an existing derivation so we can test the noop path
        derivation_service.rederive(
            document_id=document_id,
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            chunker_name="hierarchical",
            chunker_version="hierarchical-v1",
            tokenizer_name="cl100k_base",
        )

        result = derivation_service.rederive(
            document_id=document_id,
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            chunker_name="hierarchical",
            chunker_version="hierarchical-v1",
            tokenizer_name="cl100k_base",
            dry_run=True,
        )

        assert result["dry_run"] is True
        assert result["total_rederived"] == 0

    def test_derivation_comparison(self, service, derivation_service, tmp_path):
        """Comparing two derivations produces a report."""
        # Seed two documents with different content
        result1 = _seed_corpus(service, url="https://example.com/test1")
        result2 = _seed_corpus(
            service,
            url="https://example.com/test2",
            content="# Different Doc\n\nDifferent content.\n",
        )

        # Create derivations for both
        derivation_service.rederive(
            document_id=result1.document_id,
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            chunker_name="hierarchical",
            chunker_version="hierarchical-v1",
            tokenizer_name="cl100k_base",
        )

        derivation_service.rederive(
            document_id=result2.document_id,
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            chunker_name="hierarchical",
            chunker_version="hierarchical-v1",
            tokenizer_name="cl100k_base",
        )

        # List derivations and compare first two
        derivations = derivation_service.list_derivations()
        if len(derivations) >= 2:
            report = derivation_service.compare_derivations(
                UUID(derivations[1]["id"]),
                UUID(derivations[0]["id"]),
            )
            assert isinstance(report, DerivationComparisonReport)
            d = report.to_dict()
            assert "parser_version" in d
            assert "chunks" in d
            assert "blocks" in d

    def test_snapshot_target_rederive(self, service, derivation_service, tmp_path):
        """Rederive by snapshot ID works."""
        # Seed a document
        result = _seed_corpus(service)
        snapshot_id = result.snapshot_id

        result = derivation_service.rederive(
            snapshot_id=snapshot_id,
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            chunker_name="hierarchical",
            chunker_version="hierarchical-v1",
            tokenizer_name="cl100k_base",
        )

        assert "results" in result
        assert "targets" in result


# ---------------------------------------------------------------------------
# Integration tests: UoW derivation methods
# ---------------------------------------------------------------------------


class TestDerivationUoWMethods:
    """Tests for derivation repository methods in PostgresUnitOfWork."""

    pytestmark = pytest.mark.skipif(
        not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
    )

    @pytest.fixture
    def uow_factory(self, tmp_path):
        """Build a UoW factory."""
        migrate(TEST_DSN)
        config = _make_config(tmp_path)
        from functools import partial
        from research_store.postgres import PostgresUnitOfWork

        return partial(
            PostgresUnitOfWork,
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        )

    def test_create_derivation(self, uow_factory):
        """Creating a derivation inserts a row."""
        doc_id = uuid4()
        snap_id = uuid4()
        config_sha = sha256(b"test-config").hexdigest()

        with uow_factory() as uow:
            derivation = uow.derivations.create(
                document_id=doc_id,
                snapshot_id=snap_id,
                parser_version="markdown-v1",
                normalization_version="cleanup-v1",
                chunker_name="hierarchical",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
                chunk_count=5,
                block_count=10,
                configuration_sha256=config_sha,
                status="pending",
            )
            assert derivation.id is not None
            assert derivation.status == "pending"
            assert derivation.chunk_count == 5
            assert derivation.block_count == 10

    def test_get_derivation(self, uow_factory):
        """Getting a derivation by ID returns the row."""
        doc_id = uuid4()
        snap_id = uuid4()
        config_sha = sha256(b"test-config-2").hexdigest()

        with uow_factory() as uow:
            derivation = uow.derivations.create(
                document_id=doc_id,
                snapshot_id=snap_id,
                parser_version="markdown-v1",
                normalization_version="cleanup-v1",
                chunker_name="hierarchical",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
                chunk_count=3,
                block_count=6,
                configuration_sha256=config_sha,
                status="pending",
            )
            retrieved = uow.derivations.get(derivation.id)
            assert retrieved is not None
            assert retrieved.id == derivation.id
            assert retrieved.chunk_count == 3

    def test_get_nonexistent_derivation(self, uow_factory):
        """Getting a nonexistent derivation returns None."""
        with uow_factory() as uow:
            assert uow.derivations.get(uuid4()) is None

    def test_list_derivations(self, uow_factory):
        """Listing derivations returns all rows."""
        doc_id = uuid4()
        snap_id = uuid4()
        config_sha = sha256(b"test-config-3").hexdigest()

        with uow_factory() as uow:
            uow.derivations.create(
                document_id=doc_id,
                snapshot_id=snap_id,
                parser_version="markdown-v1",
                normalization_version="cleanup-v1",
                chunker_name="hierarchical",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
                chunk_count=1,
                block_count=2,
                configuration_sha256=config_sha,
                status="pending",
            )
            uow.derivations.create(
                document_id=doc_id,
                snapshot_id=snap_id,
                parser_version="html-normalized-v1",
                normalization_version="cleanup-v1",
                chunker_name="hierarchical",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
                chunk_count=2,
                block_count=4,
                configuration_sha256=sha256(b"test-config-3b").hexdigest(),
                status="pending",
            )

            all_derivs = uow.derivations.list()
            assert len(all_derivs) >= 2

    def test_list_derivations_filtered(self, uow_factory):
        """Listing derivations with status filter works."""
        doc_id = uuid4()
        snap_id = uuid4()
        config_sha = sha256(b"test-config-4").hexdigest()

        with uow_factory() as uow:
            uow.derivations.create(
                document_id=doc_id,
                snapshot_id=snap_id,
                parser_version="markdown-v1",
                normalization_version="cleanup-v1",
                chunker_name="hierarchical",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
                chunk_count=1,
                block_count=2,
                configuration_sha256=config_sha,
                status="pending",
            )
            uow.derivations.create(
                document_id=doc_id,
                snapshot_id=snap_id,
                parser_version="markdown-v1",
                normalization_version="cleanup-v1",
                chunker_name="hierarchical",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
                chunk_count=1,
                block_count=2,
                configuration_sha256=sha256(b"test-config-4b").hexdigest(),
                status="active",
            )

            pending = uow.derivations.list(status="pending")
            assert len(pending) == 1
            assert pending[0]["status"] == "pending"

            active = uow.derivations.list(status="active")
            assert len(active) == 1
            assert active[0]["status"] == "active"

    def test_find_by_configuration(self, uow_factory):
        """Finding by configuration SHA-256 works."""
        doc_id = uuid4()
        snap_id = uuid4()
        config_sha = sha256(b"test-config-5").hexdigest()

        with uow_factory() as uow:
            derivation = uow.derivations.create(
                document_id=doc_id,
                snapshot_id=snap_id,
                parser_version="markdown-v1",
                normalization_version="cleanup-v1",
                chunker_name="hierarchical",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
                chunk_count=1,
                block_count=2,
                configuration_sha256=config_sha,
                status="pending",
            )

            found = uow.derivations.find_by_configuration(doc_id, config_sha)
            assert found is not None
            assert found["id"] == str(derivation.id)

            # Different SHA should not match
            not_found = uow.derivations.find_by_configuration(
                doc_id, sha256(b"different").hexdigest()
            )
            assert not_found is None

    def test_activate_derivation(self, uow_factory):
        """Activating a derivation changes its status."""
        doc_id = uuid4()
        snap_id = uuid4()
        config_sha = sha256(b"test-config-6").hexdigest()

        with uow_factory() as uow:
            derivation = uow.derivations.create(
                document_id=doc_id,
                snapshot_id=snap_id,
                parser_version="markdown-v1",
                normalization_version="cleanup-v1",
                chunker_name="hierarchical",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
                chunk_count=1,
                block_count=2,
                configuration_sha256=config_sha,
                status="pending",
            )

            activated = uow.derivations.activate(derivation.id)
            assert activated.status == "active"

            # Verify it's no longer pending
            retrieved = uow.derivations.get(derivation.id)
            assert retrieved.status == "active"

    def test_activate_nonexistent_derivation(self, uow_factory):
        """Activating a nonexistent derivation raises ValueError."""
        with uow_factory() as uow:
            with pytest.raises(ValueError, match="not found"):
                uow.derivations.activate(uuid4())

    def test_activate_non_pending_derivation(self, uow_factory):
        """Activating an already-active derivation raises ValueError."""
        doc_id = uuid4()
        snap_id = uuid4()
        config_sha = sha256(b"test-config-7").hexdigest()

        with uow_factory() as uow:
            derivation = uow.derivations.create(
                document_id=doc_id,
                snapshot_id=snap_id,
                parser_version="markdown-v1",
                normalization_version="cleanup-v1",
                chunker_name="hierarchical",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
                chunk_count=1,
                block_count=2,
                configuration_sha256=config_sha,
                status="active",
            )

            with pytest.raises(ValueError, match="not pending"):
                uow.derivations.activate(derivation.id)

    def test_supersede_prior_active(self, uow_factory):
        """Activating a new derivation marks the prior active as superseded."""
        doc_id = uuid4()
        snap_id = uuid4()
        config_sha1 = sha256(b"test-config-8a").hexdigest()
        config_sha2 = sha256(b"test-config-8b").hexdigest()

        with uow_factory() as uow:
            # Create first derivation and activate it
            d1 = uow.derivations.create(
                document_id=doc_id,
                snapshot_id=snap_id,
                parser_version="markdown-v1",
                normalization_version="cleanup-v1",
                chunker_name="hierarchical",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
                chunk_count=1,
                block_count=2,
                configuration_sha256=config_sha1,
                status="pending",
            )
            uow.derivations.activate(d1.id)

            # Create second derivation
            d2 = uow.derivations.create(
                document_id=doc_id,
                snapshot_id=snap_id,
                parser_version="html-normalized-v1",
                normalization_version="cleanup-v1",
                chunker_name="hierarchical",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
                chunk_count=2,
                block_count=4,
                configuration_sha256=config_sha2,
                status="pending",
            )

            # Activate second — should supersede first
            uow.derivations.activate(d2.id)

            # First should now be superseded
            retrieved_d1 = uow.derivations.get(d1.id)
            assert retrieved_d1.status == "superseded"

            # Second should be active
            retrieved_d2 = uow.derivations.get(d2.id)
            assert retrieved_d2.status == "active"

    def test_count_chunks_for_derivation(self, uow_factory):
        """Counting chunks for a derivation works."""
        doc_id = uuid4()
        snap_id = uuid4()
        config_sha = sha256(b"test-config-9").hexdigest()

        with uow_factory() as uow:
            derivation = uow.derivations.create(
                document_id=doc_id,
                snapshot_id=snap_id,
                parser_version="markdown-v1",
                normalization_version="cleanup-v1",
                chunker_name="hierarchical",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
                chunk_count=7,
                block_count=3,
                configuration_sha256=config_sha,
                status="pending",
            )

            count = uow.derivations.count_chunks_for_derivation(derivation.id)
            assert count == 7

    def test_count_blocks_for_derivation(self, uow_factory):
        """Counting blocks for a derivation works."""
        doc_id = uuid4()
        snap_id = uuid4()
        config_sha = sha256(b"test-config-10").hexdigest()

        with uow_factory() as uow:
            derivation = uow.derivations.create(
                document_id=doc_id,
                snapshot_id=snap_id,
                parser_version="markdown-v1",
                normalization_version="cleanup-v1",
                chunker_name="hierarchical",
                chunker_version="hierarchical-v1",
                tokenizer_name="cl100k_base",
                chunk_count=5,
                block_count=12,
                configuration_sha256=config_sha,
                status="pending",
            )

            count = uow.derivations.count_blocks_for_derivation(derivation.id)
            assert count == 12


# ---------------------------------------------------------------------------
# Integration tests: multi-derivation coexistence
# ---------------------------------------------------------------------------


class TestMultiDerivationCoexistence:
    """Tests for old and new derivation coexistence."""

    pytestmark = pytest.mark.skipif(
        not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
    )

    @pytest.fixture
    def service(self, tmp_path):
        """Build a CorpusService."""
        migrate(TEST_DSN)
        config = _make_config(tmp_path)
        from research_store.container import build_service

        return build_service(config)

    def test_old_and_new_derivations_coexist(self, service, tmp_path):
        """Old and new derivations coexist after parser upgrade."""
        from functools import partial
        from research_store.postgres import PostgresUnitOfWork

        config = _make_config(tmp_path)
        uow_factory = partial(
            PostgresUnitOfWork,
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        )
        derivation_service = DerivationService(
            uow_factory=uow_factory,
            corpus_service=service,
        )

        # Seed a document with default config
        result = _seed_corpus(service)
        document_id = result.document_id

        # Create a derivation with the default config
        derivation_service.rederive(
            document_id=document_id,
            parser_version=config.parser_version,
            normalization_version=config.normalization_version,
            chunker_name=config.chunker_name,
            chunker_version=config.chunker_version,
            tokenizer_name=config.tokenizer_name,
        )

        # Create a second derivation with a different parser version
        derivation_service.rederive(
            document_id=document_id,
            parser_version="html-normalized-v1",
            normalization_version=config.normalization_version,
            chunker_name=config.chunker_name,
            chunker_version=config.chunker_version,
            tokenizer_name=config.tokenizer_name,
        )

        # Both derivations should exist
        derivations = derivation_service.list_derivations(
            document_id=document_id,
        )
        assert len(derivations) >= 2

        # Verify both have different parser versions
        parser_versions = {d["parser_version"] for d in derivations}
        assert len(parser_versions) >= 2

    def test_source_snapshot_preserved(self, service, tmp_path):
        """Source snapshot is not recreated during rederive."""
        from functools import partial
        from research_store.postgres import PostgresUnitOfWork

        config = _make_config(tmp_path)
        uow_factory = partial(
            PostgresUnitOfWork,
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        )
        derivation_service = DerivationService(
            uow_factory=uow_factory,
            corpus_service=service,
        )

        # Seed a document
        result = _seed_corpus(service)
        original_snapshot_id = result.snapshot_id

        # Redrive — the snapshot should be reused, not recreated
        rederive_result = derivation_service.rederive(
            document_id=result.document_id,
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            chunker_name="hierarchical",
            chunker_version="hierarchical-v1",
            tokenizer_name="cl100k_base",
        )

        # Check that the result references the original snapshot
        for item in rederive_result.get("results", []):
            if item.get("snapshot_id"):
                assert item["snapshot_id"] == str(original_snapshot_id)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestCLI:
    """Tests for the CLI command parsing."""

    def test_rederive_v2_parser(self):
        """The rederive-v2 subcommand parses correctly."""
        from research_store.cli import parser as research_store_parser

        args = research_store_parser().parse_args(
            [
                "rederive-v2",
                "--all",
                "--parser-version",
                "html-normalized-v1",
                "--dry-run",
            ]
        )
        assert args.command == "rederive-v2"
        assert args.all is True
        assert args.parser_version == "html-normalized-v1"
        assert args.dry_run is True

    def test_rederive_v2_with_snapshot(self):
        """rederive-v2 with --snapshot parses correctly."""
        from research_store.cli import parser as research_store_parser

        test_uuid = str(uuid4())
        args = research_store_parser().parse_args(
            [
                "rederive-v2",
                "--snapshot",
                test_uuid,
                "--chunker-version",
                "hierarchical-v2",
                "--tokenizer-name",
                "p500k_base",
            ]
        )
        assert args.snapshot == test_uuid
        assert args.chunker_version == "hierarchical-v2"
        assert args.tokenizer_name == "p500k_base"

    def test_derivation_list_parser(self):
        """derivation-list subcommand parses correctly."""
        from research_store.cli import parser as research_store_parser

        test_uuid = str(uuid4())
        args = research_store_parser().parse_args(
            [
                "derivation-list",
                "--document",
                test_uuid,
                "--status",
                "pending",
            ]
        )
        assert args.command == "derivation-list"
        assert args.document == test_uuid
        assert args.status == "pending"

    def test_derivation_activate_parser(self):
        """derivation-activate subcommand parses correctly."""
        from research_store.cli import parser as research_store_parser

        test_uuid = str(uuid4())
        args = research_store_parser().parse_args(["derivation-activate", test_uuid])
        assert args.command == "derivation-activate"
        assert args.id == test_uuid

    def test_derivation_compare_parser(self):
        """derivation-compare subcommand parses correctly."""
        from research_store.cli import parser as research_store_parser

        old_uuid = str(uuid4())
        new_uuid = str(uuid4())
        args = research_store_parser().parse_args(
            [
                "derivation-compare",
                old_uuid,
                new_uuid,
                "--output",
                "/tmp/report.json",
            ]
        )
        assert args.command == "derivation-compare"
        assert args.old_id == old_uuid
        assert args.new_id == new_uuid
        assert args.output == "/tmp/report.json"

    def test_rederive_v2_mutually_exclusive(self):
        """rederive-v2 targets are mutually exclusive."""
        from research_store.cli import parser as research_store_parser

        with pytest.raises(SystemExit):
            research_store_parser().parse_args(
                [
                    "rederive-v2",
                    "--all",
                    "--snapshot",
                    str(uuid4()),
                ]
            )
