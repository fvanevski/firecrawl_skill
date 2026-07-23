"""ExtractionService unit and integration tests (issue #40).

Pure unit tests run without a database.  Integration tests require
RESEARCH_STORE_TEST_DATABASE_URL and are marked with pytest.mark.skipif.
"""

from __future__ import annotations

# ruff: noqa: E402 - load the sibling script package without installing it.

import hashlib
import os
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.blob import ContentAddressedBlobStore
from research_store.config import StoreConfig
from research_store.domain import (
    BlobReference,
    ExtractionAttempt,
    ExtractionQualityMetrics,
    utcnow,
)
from research_store.extraction_service import (
    ExtractionError,
    ExtractionService,
)

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")


def _integration():
    """Skip marker for integration tests."""
    return pytest.mark.skipif(
        not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
    )


# -----------------------------------------------------------------------
# Pure unit tests (no database required)
# -----------------------------------------------------------------------


def test_extraction_quality_metrics_roundtrip():
    metrics = ExtractionQualityMetrics(
        byte_length=1024,
        visible_text_length=800,
        paragraph_count=5,
        heading_count=3,
        list_count=2,
        table_count=1,
        link_density=0.15,
        boilerplate_ratio=0.1,
        title_present=True,
        language_confidence=0.95,
        content_type_consistent=True,
        anti_bot_markers=0,
        duplicate_content_similarity=0.05,
        query_term_coverage=0.8,
        required_structured_fields=2,
        parser_warnings=0,
        code_to_prose_ratio=0.1,
        extraction_method_confidence=0.9,
        quality_version="quality-v1",
    )
    d = metrics.to_dict()
    restored = ExtractionQualityMetrics.from_dict(d)
    assert restored.byte_length == 1024
    assert restored.quality_version == "quality-v1"
    assert restored.link_density == 0.15


def test_extraction_quality_metrics_defaults():
    metrics = ExtractionQualityMetrics()
    assert metrics.byte_length == 0
    assert metrics.quality_version == "quality-v1"
    d = metrics.to_dict()
    restored = ExtractionQualityMetrics.from_dict(d)
    assert restored.byte_length == 0


def test_extraction_attempt_validation_invalid_method():
    with pytest.raises(ValueError, match="invalid extraction method"):
        ExtractionAttempt(
            id=uuid4(),
            candidate_id=uuid4(),
            run_id=uuid4(),
            invocation_id=None,
            attempt_number=1,
            method="invalid_method",
            method_version="v1",
            requested_format=None,
            start_time=utcnow(),
            end_time=None,
            exit_status="succeeded",
            http_status=None,
            backend_status=None,
            raw_blob=None,
            normalized_blob=None,
            parser_used=None,
            quality_metrics=None,
            failure_class="none",
            retry_parent_id=None,
            disposition="unassessed",
            error_message=None,
            selection_reason=None,
        )


def test_extraction_attempt_validation_invalid_status():
    with pytest.raises(ValueError, match="invalid exit_status"):
        ExtractionAttempt(
            id=uuid4(),
            candidate_id=uuid4(),
            run_id=uuid4(),
            invocation_id=None,
            attempt_number=1,
            method="firecrawl_main_content",
            method_version="v1",
            requested_format=None,
            start_time=utcnow(),
            end_time=None,
            exit_status="invalid",
            http_status=None,
            backend_status=None,
            raw_blob=None,
            normalized_blob=None,
            parser_used=None,
            quality_metrics=None,
            failure_class="none",
            retry_parent_id=None,
            disposition="unassessed",
            error_message=None,
            selection_reason=None,
        )


def test_extraction_attempt_validation_invalid_failure_class():
    with pytest.raises(ValueError, match="invalid failure_class"):
        ExtractionAttempt(
            id=uuid4(),
            candidate_id=uuid4(),
            run_id=uuid4(),
            invocation_id=None,
            attempt_number=1,
            method="firecrawl_main_content",
            method_version="v1",
            requested_format=None,
            start_time=utcnow(),
            end_time=None,
            exit_status="succeeded",
            http_status=None,
            backend_status=None,
            raw_blob=None,
            normalized_blob=None,
            parser_used=None,
            quality_metrics=None,
            failure_class="invalid",
            retry_parent_id=None,
            disposition="unassessed",
            error_message=None,
            selection_reason=None,
        )


def test_extraction_attempt_validation_invalid_disposition():
    with pytest.raises(ValueError, match="invalid disposition"):
        ExtractionAttempt(
            id=uuid4(),
            candidate_id=uuid4(),
            run_id=uuid4(),
            invocation_id=None,
            attempt_number=1,
            method="firecrawl_main_content",
            method_version="v1",
            requested_format=None,
            start_time=utcnow(),
            end_time=None,
            exit_status="succeeded",
            http_status=None,
            backend_status=None,
            raw_blob=None,
            normalized_blob=None,
            parser_used=None,
            quality_metrics=None,
            failure_class="none",
            retry_parent_id=None,
            disposition="invalid",
            error_message=None,
            selection_reason=None,
        )


def test_extraction_attempt_validation_attempt_number():
    with pytest.raises(ValueError, match="attempt_number must be >= 1"):
        ExtractionAttempt(
            id=uuid4(),
            candidate_id=uuid4(),
            run_id=uuid4(),
            invocation_id=None,
            attempt_number=0,
            method="firecrawl_main_content",
            method_version="v1",
            requested_format=None,
            start_time=utcnow(),
            end_time=None,
            exit_status="succeeded",
            http_status=None,
            backend_status=None,
            raw_blob=None,
            normalized_blob=None,
            parser_used=None,
            quality_metrics=None,
            failure_class="none",
            retry_parent_id=None,
            disposition="unassessed",
            error_message=None,
            selection_reason=None,
        )


def test_extraction_attempt_from_mapping():
    now = utcnow()
    blob = BlobReference(
        sha256="a" * 64,
        uri="blob://sha256/" + "a" * 64,
        byte_length=100,
        mime_type="text/markdown",
    )
    qm = ExtractionQualityMetrics(byte_length=100, visible_text_length=80)
    row = {
        "id": str(uuid4()),
        "candidate_id": str(uuid4()),
        "run_id": str(uuid4()),
        "invocation_id": None,
        "attempt_number": 1,
        "method": "firecrawl_main_content",
        "method_version": "v1",
        "requested_format": None,
        "start_time": now.isoformat(),
        "end_time": None,
        "exit_status": "succeeded",
        "http_status": 200,
        "backend_status": None,
        "raw_blob": {
            "sha256": blob.sha256,
            "uri": blob.uri,
            "byte_length": blob.byte_length,
            "mime_type": blob.mime_type,
        },
        "normalized_blob": None,
        "parser_used": "markdown-v1",
        "quality_metrics": qm.to_dict(),
        "failure_class": "none",
        "retry_parent_id": None,
        "disposition": "unassessed",
        "error_message": None,
        "selection_reason": None,
        "selected": False,
        "created_at": now.isoformat(),
    }
    attempt = ExtractionAttempt.from_mapping(row)
    assert attempt.attempt_number == 1
    assert attempt.method == "firecrawl_main_content"
    assert attempt.raw_blob is not None
    assert attempt.raw_blob.sha256 == blob.sha256
    assert attempt.quality_metrics is not None
    assert attempt.quality_metrics.byte_length == 100


def test_extraction_attempt_to_dict_roundtrip():
    now = utcnow()
    attempt = ExtractionAttempt(
        id=uuid4(),
        candidate_id=uuid4(),
        run_id=uuid4(),
        invocation_id=None,
        attempt_number=1,
        method="firecrawl_main_content",
        method_version="v1",
        requested_format=None,
        start_time=now,
        end_time=None,
        exit_status="succeeded",
        http_status=200,
        backend_status=None,
        raw_blob=None,
        normalized_blob=None,
        parser_used="markdown-v1",
        quality_metrics=None,
        failure_class="none",
        retry_parent_id=None,
        disposition="unassessed",
        error_message=None,
        selection_reason=None,
    )
    d = attempt.to_dict()
    assert d["method"] == "firecrawl_main_content"
    assert d["exit_status"] == "succeeded"
    assert d["attempt_number"] == 1


def test_extraction_service_requires_blob_store():
    tmp_path = Path("/tmp/test_extraction_no_blob")
    tmp_path.mkdir(exist_ok=True)
    config = replace(
        StoreConfig.from_env(),
        database_url="postgresql://localhost/test",
        blob_root=tmp_path / "blobs_disabled",
    )
    service = ExtractionService(
        uow_factory=lambda: None,
        blob_store=None,
        config=config,
    )
    with pytest.raises(ExtractionError, match="blob_store is required"):
        service.store_raw_blob(b"test content")


def test_extraction_service_store_raw_blob():
    tmp_path = Path("/tmp/test_extraction_blob")
    tmp_path.mkdir(exist_ok=True)
    blob_store = ContentAddressedBlobStore(tmp_path / "blobs")
    config = replace(
        StoreConfig.from_env(),
        database_url="postgresql://localhost/test",
        blob_root=tmp_path / "blobs",
    )
    service = ExtractionService(
        uow_factory=lambda: None,
        blob_store=blob_store,
        config=config,
    )
    content = b"Hello, extraction world!"
    ref = service.store_raw_blob(content)
    assert ref.sha256 == hashlib.sha256(content).hexdigest()
    assert ref.byte_length == len(content)


def test_extraction_service_store_normalized_blob():
    tmp_path = Path("/tmp/test_extraction_norm_blob")
    tmp_path.mkdir(exist_ok=True)
    blob_store = ContentAddressedBlobStore(tmp_path / "blobs")
    config = replace(
        StoreConfig.from_env(),
        database_url="postgresql://localhost/test",
        blob_root=tmp_path / "blobs",
    )
    service = ExtractionService(
        uow_factory=lambda: None,
        blob_store=blob_store,
        config=config,
    )
    content = b"Normalized content"
    ref = service.store_normalized_blob(content)
    assert ref.sha256 == hashlib.sha256(content).hexdigest()
    assert ref.byte_length == len(content)


# -----------------------------------------------------------------------
# Integration tests (require PostgreSQL)
# -----------------------------------------------------------------------

from research_store.postgres import connect, migrate


@pytest.fixture
def uow_factory():
    def factory():
        from research_store.postgres import PostgresUnitOfWork

        return PostgresUnitOfWork(
            TEST_DSN,
            "test_physical_collection",
            embedding_model="embed",
            embedding_revision="main",
            embedding_dimension=4,
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            chunker_version="structural-v1",
        )

    return factory


@pytest.fixture
def blob_store(tmp_path):
    return ContentAddressedBlobStore(tmp_path / "blobs")


@pytest.fixture
def extraction_service(uow_factory, blob_store, tmp_path):
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    return ExtractionService(
        uow_factory=uow_factory,
        blob_store=blob_store,
        config=config,
    )


@pytest.fixture
def sample_candidate():
    return uuid4()


@pytest.fixture
def sample_run():
    return uuid4()


@_integration()
def test_create_and_complete_successful_attempt(
    extraction_service, sample_candidate, sample_run
):
    attempt_id = extraction_service.create_attempt(
        candidate_id=sample_candidate,
        run_id=sample_run,
        method="firecrawl_main_content",
        method_version="markdown-v1",
    )
    assert attempt_id is not None

    content = b"# Hello World\n\nThis is a test document."
    raw_ref = extraction_service.store_raw_blob(content)
    normalized_ref = extraction_service.store_normalized_blob(content)

    quality = ExtractionQualityMetrics(
        byte_length=len(content),
        visible_text_length=len(content) - 20,
        paragraph_count=1,
        heading_count=1,
        extraction_method_confidence=0.95,
    )

    attempt = extraction_service.complete_attempt(
        attempt_id=attempt_id,
        exit_status="succeeded",
        raw_blob=raw_ref,
        normalized_blob=normalized_ref,
        parser_used="markdown-v1",
        quality_metrics=quality,
        failure_class="none",
        http_status=200,
    )

    assert attempt.exit_status == "succeeded"
    assert attempt.raw_blob is not None
    assert attempt.raw_blob.sha256 == raw_ref.sha256
    assert attempt.normalized_blob is not None
    assert attempt.normalized_blob.sha256 == normalized_ref.sha256


@_integration()
def test_multiple_ordered_attempts_per_candidate(
    extraction_service, sample_candidate, sample_run
):
    ids = []
    for i in range(3):
        aid = extraction_service.create_attempt(
            candidate_id=sample_candidate,
            run_id=sample_run,
            method="firecrawl_main_content",
            method_version=f"v{i + 1}",
        )
        ids.append(aid)

    attempts = extraction_service.list_attempts(sample_candidate, run_id=sample_run)
    assert len(attempts) == 3
    numbers = [a.attempt_number for a in attempts]
    assert numbers == [1, 2, 3]


@_integration()
def test_failed_then_successful_retry(
    extraction_service, sample_candidate, sample_run
):
    fail_id = extraction_service.create_attempt(
        candidate_id=sample_candidate,
        run_id=sample_run,
        method="firecrawl_main_content",
    )
    extraction_service.complete_attempt(
        attempt_id=fail_id,
        exit_status="failed",
        failure_class="anti_bot",
        error_message="Cloudflare challenge detected",
    )

    retry_id = extraction_service.create_retry(
        candidate_id=sample_candidate,
        run_id=sample_run,
        parent_attempt_id=fail_id,
        method="firecrawl_full_page",
    )

    content = b"Retrieved content after retry"
    ref = extraction_service.store_raw_blob(content)
    extraction_service.complete_attempt(
        attempt_id=retry_id,
        exit_status="succeeded",
        raw_blob=ref,
        failure_class="none",
    )

    attempts = extraction_service.list_attempts(sample_candidate, run_id=sample_run)
    assert len(attempts) == 2
    assert attempts[0].exit_status == "failed"
    assert attempts[1].exit_status == "succeeded"
    assert attempts[1].retry_parent_id == fail_id


@_integration()
def test_partial_failure_attempt(
    extraction_service, sample_candidate, sample_run
):
    aid = extraction_service.create_attempt(
        candidate_id=sample_candidate,
        run_id=sample_run,
    )
    extraction_service.complete_attempt(
        attempt_id=aid,
        exit_status="partial",
        failure_class="parser",
        error_message="Some content extracted, but tables failed",
    )

    attempts = extraction_service.list_attempts(sample_candidate, run_id=sample_run)
    assert len(attempts) == 1
    assert attempts[0].exit_status == "partial"


@_integration()
def test_select_final_attempt(
    extraction_service, sample_candidate, sample_run
):
    aid1 = extraction_service.create_attempt(
        candidate_id=sample_candidate,
        run_id=sample_run,
    )
    ref1 = extraction_service.store_raw_blob(b"content 1")
    extraction_service.complete_attempt(
        attempt_id=aid1, exit_status="succeeded", raw_blob=ref1
    )

    extraction_service.select_final_attempt(
        candidate_id=sample_candidate,
        attempt_id=aid1,
        selection_reason="best quality metrics",
    )

    selected = extraction_service.get_selected_attempt(sample_candidate)
    assert selected is not None
    assert selected.id == aid1
    assert selected.selection_reason == "best quality metrics"

    aid2 = extraction_service.create_attempt(
        candidate_id=sample_candidate,
        run_id=sample_run,
    )
    ref2 = extraction_service.store_raw_blob(b"content 2")
    extraction_service.complete_attempt(
        attempt_id=aid2, exit_status="succeeded", raw_blob=ref2
    )

    extraction_service.select_final_attempt(
        candidate_id=sample_candidate,
        attempt_id=aid2,
        selection_reason="higher confidence",
    )

    selected = extraction_service.get_selected_attempt(sample_candidate)
    assert selected.id == aid2
    assert selected.selection_reason == "higher confidence"

    attempts = extraction_service.list_attempts(sample_candidate, run_id=sample_run)
    assert len(attempts) == 2


@_integration()
def test_concise_valid_content_accepted(
    extraction_service, sample_candidate, sample_run
):
    aid = extraction_service.create_attempt(
        candidate_id=sample_candidate,
        run_id=sample_run,
    )
    content = b"Short official notice: meeting at 3pm."
    ref = extraction_service.store_raw_blob(content)

    quality = ExtractionQualityMetrics(
        byte_length=len(content),
        visible_text_length=len(content),
        paragraph_count=1,
        heading_count=0,
        title_present=True,
        content_type_consistent=True,
        extraction_method_confidence=0.8,
    )

    extraction_service.complete_attempt(
        attempt_id=aid,
        exit_status="succeeded",
        raw_blob=ref,
        quality_metrics=quality,
        failure_class="none",
    )
    extraction_service.evaluate_and_set_disposition(
        attempt_id=aid,
        quality_metrics=quality,
        disposition="acceptable",
    )

    attempts = extraction_service.list_attempts(sample_candidate, run_id=sample_run)
    assert len(attempts) == 1
    assert attempts[0].disposition == "acceptable"


@_integration()
def test_long_anti_bot_content_rejected(
    extraction_service, sample_candidate, sample_run
):
    aid = extraction_service.create_attempt(
        candidate_id=sample_candidate,
        run_id=sample_run,
    )
    content = (
        b"<html><body><div class='cf-challenge'>"
        b"Please verify you are human. This page is protected by Cloudflare."
        b"</div></body></html>"
    )
    ref = extraction_service.store_raw_blob(content)

    quality = ExtractionQualityMetrics(
        byte_length=len(content),
        visible_text_length=0,
        paragraph_count=0,
        heading_count=0,
        anti_bot_markers=5,
        extraction_method_confidence=0.1,
    )

    extraction_service.complete_attempt(
        attempt_id=aid,
        exit_status="failed",
        raw_blob=ref,
        quality_metrics=quality,
        failure_class="anti_bot",
        error_message="Anti-bot challenge detected",
    )

    attempts = extraction_service.list_attempts(sample_candidate, run_id=sample_run)
    assert len(attempts) == 1
    assert attempts[0].failure_class == "anti_bot"
    assert attempts[0].exit_status == "failed"


@_integration()
def test_ambiguous_content_disposition(
    extraction_service, sample_candidate, sample_run
):
    aid = extraction_service.create_attempt(
        candidate_id=sample_candidate,
        run_id=sample_run,
    )
    content = b"Some text but mostly navigation and footer links."
    ref = extraction_service.store_raw_blob(content)

    quality = ExtractionQualityMetrics(
        byte_length=len(content),
        visible_text_length=50,
        paragraph_count=0,
        heading_count=0,
        link_density=0.8,
        boilerplate_ratio=0.7,
        extraction_method_confidence=0.4,
    )

    extraction_service.complete_attempt(
        attempt_id=aid,
        exit_status="succeeded",
        raw_blob=ref,
        quality_metrics=quality,
        failure_class="none",
    )
    extraction_service.evaluate_and_set_disposition(
        attempt_id=aid,
        quality_metrics=quality,
        disposition="ambiguous",
    )

    attempts = extraction_service.list_attempts(sample_candidate, run_id=sample_run)
    assert len(attempts) == 1
    assert attempts[0].disposition == "ambiguous"


@_integration()
def test_malformed_html_handling(
    extraction_service, sample_candidate, sample_run
):
    aid = extraction_service.create_attempt(
        candidate_id=sample_candidate,
        run_id=sample_run,
    )
    content = b"<p>Unclosed paragraph <div>Another unclosed <span>"
    ref = extraction_service.store_raw_blob(content)

    extraction_service.complete_attempt(
        attempt_id=aid,
        exit_status="partial",
        raw_blob=ref,
        quality_metrics=ExtractionQualityMetrics(visible_text_length=20),
        failure_class="parser",
        error_message="Malformed HTML, partial extraction",
    )

    attempts = extraction_service.list_attempts(sample_candidate, run_id=sample_run)
    assert len(attempts) == 1
    assert attempts[0].failure_class == "parser"


@_integration()
def test_unsupported_mime_type(
    extraction_service, sample_candidate, sample_run
):
    aid = extraction_service.create_attempt(
        candidate_id=sample_candidate,
        run_id=sample_run,
    )
    content = b"%PDF-1.4 binary data"
    ref = extraction_service.store_raw_blob(content)

    extraction_service.complete_attempt(
        attempt_id=aid,
        exit_status="failed",
        raw_blob=ref,
        failure_class="unsupported_format",
        http_status=200,
        error_message="Unsupported MIME type: application/pdf",
    )

    attempts = extraction_service.list_attempts(sample_candidate, run_id=sample_run)
    assert len(attempts) == 1
    assert attempts[0].failure_class == "unsupported_format"


@_integration()
def test_deterministic_blob_reference(
    extraction_service, sample_candidate, sample_run
):
    content = b"Same content for deterministic testing"
    ref1 = extraction_service.store_raw_blob(content)
    ref2 = extraction_service.store_raw_blob(content)
    assert ref1.sha256 == ref2.sha256
    assert ref1.uri == ref2.uri


@_integration()
def test_failed_attempt_not_selected(
    extraction_service, sample_candidate, sample_run
):
    aid = extraction_service.create_attempt(
        candidate_id=sample_candidate,
        run_id=sample_run,
    )
    extraction_service.complete_attempt(
        attempt_id=aid, exit_status="failed", failure_class="internal"
    )

    selected = extraction_service.get_selected_attempt(sample_candidate)
    assert selected is None

    attempts = extraction_service.list_attempts(sample_candidate, run_id=sample_run)
    assert len(attempts) == 1
    assert attempts[0].exit_status == "failed"
    assert attempts[0].disposition == "unassessed"


# -----------------------------------------------------------------------
# Migration tests
# -----------------------------------------------------------------------


@_integration()
def test_migration_fresh_database():
    """Test that migration 0021 works on a fresh database."""
    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")

    version = migrate(TEST_DSN, "0021_extraction_attempts")
    assert version == 21

    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('extraction_attempts')")
        assert cur.fetchone()[0] == "extraction_attempts"

        cur.execute(
            """SELECT column_name FROM information_schema.columns
            WHERE table_name = 'extraction_attempts'
            ORDER BY ordinal_position"""
        )
        columns = [row[0] for row in cur.fetchall()]
        assert "candidate_id" in columns
        assert "run_id" in columns
        assert "invocation_id" in columns
        assert "attempt_number" in columns
        assert "method" in columns
        assert "method_version" in columns
        assert "raw_blob_sha256" in columns
        assert "normalized_blob_sha256" in columns
        assert "quality_metrics" in columns
        assert "retry_parent_id" in columns
        assert "disposition" in columns
        assert "selected" in columns


@_integration()
def test_migration_upgrade_from_main():
    """Test that migration 0021 upgrades from current main (0020)."""
    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")

    version = migrate(TEST_DSN, "head")
    assert version == 20

    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('extraction_attempts')")
        assert cur.fetchone()[0] is None

    version = migrate(TEST_DSN, "0021_extraction_attempts")
    assert version == 21

    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('extraction_attempts')")
        assert cur.fetchone()[0] == "extraction_attempts"


@_integration()
def test_migration_preserves_existing_data():
    """Test that migration 0021 does not affect existing Phase 1-4 data."""
    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE")
        cur.execute("CREATE SCHEMA public")

    version = migrate(TEST_DSN, "head")
    assert version == 20

    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sources(canonical_url) VALUES (%s)",
            ("https://example.com/test",),
        )
        cur.execute("SELECT COUNT(*) FROM sources")
        before = cur.fetchone()[0]

    version = migrate(TEST_DSN, "0021_extraction_attempts")
    assert version == 21

    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM sources")
        after = cur.fetchone()[0]
        assert after == before

        cur.execute("SELECT to_regclass('extraction_attempts')")
        assert cur.fetchone()[0] == "extraction_attempts"


# -----------------------------------------------------------------------
# Attempt ordering and retry lineage
# -----------------------------------------------------------------------


@_integration()
def test_attempt_ordering_by_number(
    extraction_service, sample_candidate, sample_run
):
    """Attempts must be ordered by attempt_number, not creation time."""
    ids = []
    for i in range(5):
        aid = extraction_service.create_attempt(
            candidate_id=sample_candidate,
            run_id=sample_run,
        )
        ids.append(aid)

    attempts = extraction_service.list_attempts(sample_candidate, run_id=sample_run)
    assert len(attempts) == 5
    for i, a in enumerate(attempts):
        assert a.attempt_number == i + 1


@_integration()
def test_retry_parent_relationship(
    extraction_service, sample_candidate, sample_run
):
    """Retry attempts must link to their parent via retry_parent_id."""
    parent_id = extraction_service.create_attempt(
        candidate_id=sample_candidate,
        run_id=sample_run,
        method="firecrawl_main_content",
    )
    extraction_service.complete_attempt(
        attempt_id=parent_id, exit_status="failed", failure_class="timeout"
    )

    retry_id = extraction_service.create_retry(
        candidate_id=sample_candidate,
        run_id=sample_run,
        parent_attempt_id=parent_id,
        method="firecrawl_full_page",
    )

    attempts = extraction_service.list_attempts(sample_candidate, run_id=sample_run)
    assert len(attempts) == 2
    parent = attempts[0]
    retry = attempts[1]
    assert parent.id == parent_id
    assert retry.id == retry_id
    assert retry.retry_parent_id == parent_id
    assert parent.retry_parent_id is None


@_integration()
def test_list_attempts_for_run(extraction_service, sample_candidate, sample_run):
    """List attempts filtered by run."""
    extraction_service.create_attempt(
        candidate_id=sample_candidate,
        run_id=sample_run,
    )
    extraction_service.create_attempt(
        candidate_id=sample_candidate,
        run_id=sample_run,
    )

    attempts = extraction_service.list_attempts(sample_candidate, run_id=sample_run)
    assert len(attempts) == 2

    for a in attempts:
        extraction_service.complete_attempt(
            attempt_id=a.id, exit_status="succeeded"
        )

    with extraction_service.uow_factory() as uow:
        filtered = uow.extraction_attempts.list_attempts_for_run(
            run_id=sample_run, exit_status="succeeded"
        )
        assert len(filtered) == 2


# -----------------------------------------------------------------------
# Quality metrics and disposition
# -----------------------------------------------------------------------


@_integration()
def test_quality_metrics_and_disposition(
    extraction_service, sample_candidate, sample_run
):
    """Quality metrics and disposition are stored and queryable."""
    aid = extraction_service.create_attempt(
        candidate_id=sample_candidate,
        run_id=sample_run,
    )

    quality = ExtractionQualityMetrics(
        byte_length=500,
        visible_text_length=400,
        paragraph_count=3,
        heading_count=2,
        extraction_method_confidence=0.85,
    )

    extraction_service.complete_attempt(
        attempt_id=aid,
        exit_status="succeeded",
        quality_metrics=quality,
        failure_class="none",
    )

    extraction_service.evaluate_and_set_disposition(
        attempt_id=aid,
        quality_metrics=quality,
        disposition="acceptable",
    )

    attempts = extraction_service.list_attempts(sample_candidate, run_id=sample_run)
    assert len(attempts) == 1
    assert attempts[0].disposition == "acceptable"
    assert attempts[0].quality_metrics is not None
    assert attempts[0].quality_metrics.byte_length == 500


# -----------------------------------------------------------------------
# Failure paths
# -----------------------------------------------------------------------


@_integration()
def test_no_selection_on_failed_attempt(
    extraction_service, sample_candidate, sample_run
):
    """Failed attempts should not be selected as final."""
    aid = extraction_service.create_attempt(
        candidate_id=sample_candidate,
        run_id=sample_run,
    )
    extraction_service.complete_attempt(
        attempt_id=aid, exit_status="failed", failure_class="network"
    )

    selected = extraction_service.get_selected_attempt(sample_candidate)
    assert selected is None


@_integration()
def test_no_selection_on_partial_attempt(
    extraction_service, sample_candidate, sample_run
):
    """Partial attempts should not be selected as final."""
    aid = extraction_service.create_attempt(
        candidate_id=sample_candidate,
        run_id=sample_run,
    )
    extraction_service.complete_attempt(
        attempt_id=aid, exit_status="partial", failure_class="parser"
    )

    selected = extraction_service.get_selected_attempt(sample_candidate)
    assert selected is None


@_integration()
def test_multiple_retries_preserve_history(
    extraction_service, sample_candidate, sample_run
):
    """Multiple retries preserve full lineage chain."""
    ids = []
    for i in range(4):
        if i == 0:
            aid = extraction_service.create_attempt(
                candidate_id=sample_candidate,
                run_id=sample_run,
            )
        else:
            aid = extraction_service.create_retry(
                candidate_id=sample_candidate,
                run_id=sample_run,
                parent_attempt_id=ids[-1],
            )
        ids.append(aid)

    attempts = extraction_service.list_attempts(sample_candidate, run_id=sample_run)
    assert len(attempts) == 4
    assert attempts[0].retry_parent_id is None
    for i in range(1, 4):
        assert attempts[i].retry_parent_id == attempts[i - 1].id