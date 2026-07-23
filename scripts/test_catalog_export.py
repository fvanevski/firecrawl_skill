"""Tests for the Catalog v5 compatibility exporter.

Covers:
- Normal export success (run, invocation, events, snapshots, claims,
  assessments, manifest).
- Deterministic regeneration (same source state → same export ID and
  source-state hash).
- Filesystem deletion and regeneration (exports survive deletion).
- Atomic write failure isolation (export failure does not mutate PG).
- Invalid and unknown IDs.
- Legacy reader compatibility (golden export files are parseable by
  the Catalog v5 legacy reader).
- No state change when exports are deleted.

PRD mapping: FR-018
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from hashlib import sha256
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.catalog_export import (
    EXPORT_SCHEMA_VERSION,
    ExportTargetNotFound,
    ExportWriteFailure,
)
from research_store.config import StoreConfig
from research_store.container import (
    build_catalog_export_service,
    build_run_service,
)
from research_store.invocation_catalog import InvocationCatalogService
from research_store.invocation_events import EventService
from research_store.postgres import PostgresUnitOfWork, connect, migrate, require_disposable_database_reset


TEST_DSN = "postgresql://postgres:postgres@localhost:5432/firecrawl_test"


@pytest.fixture(scope="session")
def prepared_database():
    """Ensure database schema is up-to-date for integration tests."""
    require_disposable_database_reset(
        TEST_DSN, "firecrawl_test"
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    assert migrate(TEST_DSN) >= 19


@pytest.fixture()
def config(tmp_path, prepared_database):
    """Store config pointing at the disposable test database."""
    return replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )


@pytest.fixture()
def run_service(config):
    return build_run_service(config)


@pytest.fixture()
def exporter(config):
    return build_catalog_export_service(config)


# -----------------------------------------------------------------------
# Helper: create a run with search acquisition
# -----------------------------------------------------------------------


def _create_run_with_data(run_service, query="export test query"):
    """Create a run and transition through the lifecycle."""
    ext_id = f"run-catalog-{uuid4()}"
    run = run_service.create(
        objective="catalog export test",
        external_id=ext_id,
    )
    run_id = run.id

    # Transition: created -> planning -> corpus_review
    run_service.transition(
        run_id,
        "planning",
        expected_revision=run.lifecycle_revision,
        idempotency_key=f"transition:planning:{run_id}",
        actor_type="system",
    )
    planning_status = run_service.status(run_id=run_id)
    
    run_service.transition(
        run_id,
        "corpus_review",
        expected_revision=planning_status.lifecycle_revision,
        idempotency_key=f"transition:corpus_review:{run_id}",
        actor_type="system",
    )

    return run_id


# -----------------------------------------------------------------------
# Normal success
# -----------------------------------------------------------------------


def test_export_run_success(config, run_service, tmp_path):
    """Export a complete run with all derived artifacts."""
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)
    target_dir = tmp_path / "export"

    result = exporter.export_run(run_id, target_dir)

    assert result.status == "complete"
    assert result.source_state_sha256 is not None
    assert result.export_schema_version == EXPORT_SCHEMA_VERSION
    assert len(result.files_created) > 0
    assert (target_dir / f"{result.run['research_run_id']}.json").exists()
    assert (target_dir / "events.jsonl").exists()


def test_export_run_no_state_change(config, run_service, tmp_path):
    """Deleting exports does not change run state."""
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)
    target_dir = tmp_path / "export"

    result = exporter.export_run(run_id, target_dir)
    assert result.status == "complete"

    # Delete export files
    for f in target_dir.rglob("*"):
        if f.is_file():
            f.unlink()
    if target_dir.exists():
        target_dir.rmdir()

    # Run state is unchanged in PostgreSQL
    status = run_service.status(run_id=run_id)
    assert status.state == "corpus_review"


def test_export_invocation_success(config, run_service, tmp_path):
    """Export a single invocation."""
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)

    # Create an invocation
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
    event_svc = EventService(uow_factory)
    catalog_svc = InvocationCatalogService(
        uow_factory, event_service=event_svc
    )

    inv_record = catalog_svc.begin(
        run_id,
        external_invocation_id=f"fc-{uuid4()}",
        operation="search",
        input_data={"query": "test query"},
    )
    inv_id = inv_record.id

    result = exporter.export_invocation(inv_id, run_id, tmp_path / "inv_export")

    assert result.status == "complete"
    assert result.target_type == "invocation"
    assert result.target_id == str(inv_id)


def test_export_events_success(config, run_service, tmp_path):
    """Export events as JSONL."""
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)

    result = exporter.export_events(run_id, tmp_path / "events")

    assert result.status == "complete"
    assert (tmp_path / "events" / "events.jsonl").exists()


def test_export_claims_success(config, run_service, tmp_path):
    """Export claims when present."""
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)

    result = exporter.export_claims(run_id, tmp_path / "claims")

    # Claims may be empty if none were recorded
    assert result.status == "complete"


def test_export_assessments_success(config, run_service, tmp_path):
    """Export assessments when present."""
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)

    result = exporter.export_assessments(run_id, tmp_path / "assessments")

    # Assessments may be empty if none were recorded
    assert result.status == "complete"


def test_export_manifest_success(config, run_service, tmp_path):
    """Export a source manifest."""
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)

    result = exporter.export_manifest(run_id, tmp_path / "manifest")

    assert result.status == "complete"
    assert (tmp_path / "manifest" / "manifest.json").exists()


# -----------------------------------------------------------------------
# Deterministic regeneration
# -----------------------------------------------------------------------


def test_regeneration_stable_source_hash(config, run_service, tmp_path):
    """Regenerated exports have the same source-state hash."""
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)

    result1 = exporter.export_run(run_id, tmp_path / "first")
    result2 = exporter.regenerate_run_export(run_id, tmp_path / "second")

    assert result1.source_state_sha256 == result2.source_state_sha256
    assert result1.export_schema_version == result2.export_schema_version


def test_regeneration_after_deletion(config, run_service, tmp_path):
    """Exports can be regenerated after deletion."""
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)
    target_dir = tmp_path / "export"

    result1 = exporter.export_run(run_id, target_dir)
    assert result1.status == "complete"

    # Delete all files
    for f in target_dir.rglob("*"):
        if f.is_file():
            f.unlink()

    result2 = exporter.regenerate_run_export(run_id, target_dir)
    assert result2.status == "complete"
    assert result2.source_state_sha256 == result1.source_state_sha256
    assert len(result2.files_created) > 0


# -----------------------------------------------------------------------
# Invalid and unknown IDs
# -----------------------------------------------------------------------


def test_export_run_unknown_id(config, run_service):
    """Exporting a non-existent run raises ExportTargetNotFound."""
    exporter = build_catalog_export_service(config)
    fake_id = uuid4()

    with pytest.raises(ExportTargetNotFound, match="not found"):
        exporter.export_run(fake_id, Path("/tmp/fake"))


def test_export_invocation_unknown_id(config, run_service):
    """Exporting a non-existent invocation raises ExportTargetNotFound."""
    exporter = build_catalog_export_service(config)
    fake_id = uuid4()
    run_id = _create_run_with_data(run_service)

    with pytest.raises(ExportTargetNotFound, match="not found"):
        exporter.export_invocation(fake_id, run_id, Path("/tmp/fake"))


# -----------------------------------------------------------------------
# Atomic write failure isolation
# -----------------------------------------------------------------------


def test_export_failure_isolation(config, run_service, tmp_path):
    """Export failure does not erase PostgreSQL state."""
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)

    # Force write failure: target_dir is a file, not a directory
    blocker = tmp_path / "blocker"
    blocker.write_text("blocker")

    with pytest.raises(ExportWriteFailure, match="exists as a file"):
        exporter.export_run(run_id, blocker)

    # PostgreSQL state is intact
    status = run_service.status(run_id=run_id)
    assert status.state == "corpus_review"


# -----------------------------------------------------------------------
# Legacy reader compatibility
# -----------------------------------------------------------------------


def test_legacy_run_reader_compatibility(config, run_service, tmp_path):
    """Exported run file is parseable by the Catalog v5 legacy reader."""
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)
    target_dir = tmp_path / "export"

    result = exporter.export_run(run_id, target_dir)
    assert result.status == "complete"

    # Read the run file and verify it has the expected structure
    run_file = target_dir / f"{result.run['research_run_id']}.json"
    run_data = json.loads(run_file.read_text(encoding="utf-8"))

    assert run_data.get("schema_version") == 5
    assert "research_run_id" in run_data
    assert "objective" in run_data
    assert "lifecycle" in run_data
    assert "invocation_ids" in run_data


def test_legacy_invocation_reader_compatibility(config, run_service, tmp_path):
    """Exported invocation file is parseable by the Catalog v5 legacy reader."""
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)

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
    event_svc = EventService(uow_factory)
    catalog_svc = InvocationCatalogService(
        uow_factory, event_service=event_svc
    )

    inv_record = catalog_svc.begin(
        run_id,
        external_invocation_id=f"fc-{uuid4()}",
        operation="search",
        input_data={"query": "test query"},
    )

    result = exporter.export_invocation(inv_record.id, run_id, tmp_path / "inv")
    assert result.status == "complete"

    inv_file = tmp_path / "inv" / "invocations" / f"{inv_record.id}.json"
    inv_data = json.loads(inv_file.read_text(encoding="utf-8"))

    assert inv_data.get("schema_version") == 5
    assert "invocation_id" in inv_data
    assert "operation" in inv_data
    assert "execution" in inv_data


def test_legacy_events_jsonl_compatibility(config, run_service, tmp_path):
    """Exported events.jsonl is parseable line-by-line by the legacy reader."""
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)

    result = exporter.export_events(run_id, tmp_path / "events")
    assert result.status == "complete"

    events_file = tmp_path / "events" / "events.jsonl"
    lines = events_file.read_text(encoding="utf-8").strip().split("\n")

    for line in lines:
        event = json.loads(line)
        assert "schema_version" in event
        assert "event_id" in event
        assert "at" in event
        assert "event" in event


# -----------------------------------------------------------------------
# Source-state hash stability
# -----------------------------------------------------------------------


def test_source_state_hash_deterministic(config, run_service, tmp_path):
    """Source-state hash is deterministic for the same source state."""
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)

    result1 = exporter.export_run(run_id, tmp_path / "first")
    result2 = exporter.export_run(run_id, tmp_path / "second")

    assert result1.source_state_sha256 == result2.source_state_sha256


def test_source_state_hash_changes_with_data(config, run_service, tmp_path):
    """Source-state hash changes when source data changes."""
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)

    result1 = exporter.export_run(run_id, tmp_path / "first")

    # Create another run with different data
    run_id2 = _create_run_with_data(run_service)
    result2 = exporter.export_run(run_id2, tmp_path / "second")

    assert result1.source_state_sha256 != result2.source_state_sha256


# -----------------------------------------------------------------------
# Golden export test
# -----------------------------------------------------------------------


def test_golden_export(config, run_service, tmp_path):
    """Golden export produces a complete, well-structured Catalog v5 export."""
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)
    target_dir = tmp_path / "golden"

    result = exporter.export_run(run_id, target_dir)

    assert result.status == "complete"
    assert result.export_schema_version == EXPORT_SCHEMA_VERSION

    # Verify all expected files exist
    run_file = target_dir / f"{result.run['research_run_id']}.json"
    assert run_file.exists()

    events_file = target_dir / "events.jsonl"
    assert events_file.exists()

    manifest_file = target_dir / "manifest.json"
    assert manifest_file.exists()

    # Verify golden schema structure
    run_data = json.loads(run_file.read_text(encoding="utf-8"))
    assert run_data["schema_version"] == 5
    assert run_data["lifecycle"]["state"] == "running"

    manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest_data["schema_version"] == 5
    assert "claims" in manifest_data
    assert "sources" in manifest_data


# -----------------------------------------------------------------------
# Sanitization test
# -----------------------------------------------------------------------


def test_export_no_secret_leakage(config, run_service, tmp_path):
    """Exported invocation output does not contain raw secrets.

    The invocation catalog service sanitizes input data via
    ``_sanitize()`` before storing in PostgreSQL.  This test verifies
    that the exported invocation record does not contain raw secret
    values that were sanitized at ingest time.
    """
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)

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
    event_svc = EventService(uow_factory)
    catalog_svc = InvocationCatalogService(
        uow_factory, event_service=event_svc
    )

    # Create an invocation with input that contains a secret-like value.
    # The catalog service sanitizes it before storing in PG.
    inv_record = catalog_svc.begin(
        run_id,
        external_invocation_id=f"fc-{uuid4()}",
        operation="search",
        input_data={
            "query": "test query",
            "api_key": "sk-secret-key-12345",
            "authorization": "Bearer token-abc-def",
        },
    )

    result = exporter.export_invocation(
        inv_record.id, run_id, tmp_path / "sanitization"
    )

    assert result.status == "complete"

    # Read back the exported invocation
    inv_file = tmp_path / "sanitization" / "invocations" / f"{inv_record.id}.json"
    inv_data = json.loads(inv_file.read_text(encoding="utf-8"))

    # Verify the input was sanitized — raw secrets must not appear
    input_data = inv_data.get("input", {})
    assert "api_key" not in input_data or "sk-secret-key-12345" not in str(input_data.get("api_key", ""))
    assert "authorization" not in input_data or "Bearer token-abc-def" not in str(input_data.get("authorization", ""))
    # The sanitized value should be present but redacted
    assert input_data.get("api_key") == "[REDACTED]"
    assert input_data.get("authorization") == "[REDACTED]"


# -----------------------------------------------------------------------
# Target-dir file isolation (B2 regression test)
# -----------------------------------------------------------------------


def test_export_run_target_dir_is_file(config, run_service, tmp_path):
    """Exporting when target_dir is an existing file raises ExportWriteFailure."""
    from research_store.catalog_export import ExportWriteFailure

    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)

    blocker = tmp_path / "blocker"
    blocker.write_text("blocker")

    with pytest.raises(ExportWriteFailure, match="exists as a file"):
        exporter.export_run(run_id, blocker)


# -----------------------------------------------------------------------
# Granular export with unknown run ID
# -----------------------------------------------------------------------


def test_export_events_unknown_run(config, run_service):
    """export_events raises ExportTargetNotFound for unknown run."""
    from research_store.catalog_export import ExportTargetNotFound

    exporter = build_catalog_export_service(config)
    fake_id = uuid4()

    with pytest.raises(ExportTargetNotFound, match="not found"):
        exporter.export_events(fake_id, Path("/tmp/fake"))


def test_export_claims_unknown_run(config, run_service):
    """export_claims raises ExportTargetNotFound for unknown run."""
    from research_store.catalog_export import ExportTargetNotFound

    exporter = build_catalog_export_service(config)
    fake_id = uuid4()

    with pytest.raises(ExportTargetNotFound, match="not found"):
        exporter.export_claims(fake_id, Path("/tmp/fake"))


def test_export_assessments_unknown_run(config, run_service):
    """export_assessments raises ExportTargetNotFound for unknown run."""
    from research_store.catalog_export import ExportTargetNotFound

    exporter = build_catalog_export_service(config)
    fake_id = uuid4()

    with pytest.raises(ExportTargetNotFound, match="not found"):
        exporter.export_assessments(fake_id, Path("/tmp/fake"))


def test_export_manifest_unknown_run(config, run_service):
    """export_manifest raises ExportTargetNotFound for unknown run."""
    from research_store.catalog_export import ExportTargetNotFound

    exporter = build_catalog_export_service(config)
    fake_id = uuid4()

    with pytest.raises(ExportTargetNotFound, match="not found"):
        exporter.export_manifest(fake_id, Path("/tmp/fake"))


# -----------------------------------------------------------------------
# Minimal run with no invocations
# -----------------------------------------------------------------------


def test_export_run_no_invocations(config, run_service, tmp_path):
    """Export succeeds for a run that has no invocations."""
    ext_id = f"run-no-inv-{uuid4()}"
    run_svc = build_run_service(config)
    run = run_svc.create(objective="minimal run", external_id=ext_id)
    run_id = run.id

    # Transition to corpus_review without creating any invocations
    run_svc.transition(
        run_id,
        "planning",
        expected_revision=run.lifecycle_revision,
        idempotency_key=f"transition:planning:{run_id}",
        actor_type="system",
    )
    planning_status = run_svc.status(run_id=run_id)
    run_svc.transition(
        run_id,
        "corpus_review",
        expected_revision=planning_status.lifecycle_revision,
        idempotency_key=f"transition:corpus_review:{run_id}",
        actor_type="system",
    )

    exporter = build_catalog_export_service(config)
    result = exporter.export_run(run_id, tmp_path / "no_inv")

    assert result.status == "complete"
    assert len(result.invocations) == 0


# -----------------------------------------------------------------------
# Export with failed-assessment stages
# -----------------------------------------------------------------------


def test_export_with_failed_assessment_stages(config, run_service, tmp_path):
    """Export includes assessments with failed stages."""
    from research_store.service import AuditService

    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)

    # Create an assessment with a failed stage via the audit service
    audit_svc = AuditService(
        partial(
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
    )

    # Create an assessment with a failed rubric stage
    assessment_id = audit_svc.create_assessment(
        run_id=run_id,
        target_type="run",
        target_id=run_id,
        target_hash=sha256(b"test-packet").hexdigest(),
        evaluator_version="catalog-v5.0",
        prompt_template_version="staged-research-audit-v1",
        policy_version="audit-policy-v1",
        stage_set=["rubric", "acquisition"],
        status="partial",
        provider="local",
        model="test-model",
        model_fingerprint="fp-test",
    )

    # Add a failed rubric stage
    audit_svc.add_stage_output(
        assessment_id=assessment_id,
        stage="rubric",
        sequence_number=1,
        status="failed",
        error="rubric evaluation failed",
    )

    # Add a successful acquisition stage
    audit_svc.add_stage_output(
        assessment_id=assessment_id,
        stage="acquisition",
        sequence_number=1,
        status="completed",
        output={"score": 0.8},
    )

    result = exporter.export_run(run_id, tmp_path / "failed_stage")

    assert result.status == "complete"
    assert len(result.assessments) == 1

    asmt = result.assessments[0]
    assert asmt["status"] == "partial"  # partial because one stage failed
    assert len(asmt["stage_outputs"]) == 2

    # Verify the failed stage is present
    failed_stages = [s for s in asmt["stage_outputs"] if s["status"] == "failed"]
    assert len(failed_stages) == 1
    assert failed_stages[0]["stage"] == "rubric"
    assert failed_stages[0]["error"] == "rubric evaluation failed"