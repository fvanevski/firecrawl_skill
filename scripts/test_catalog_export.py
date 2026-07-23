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

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from hashlib import sha256
from io import BytesIO
import json
import os
import shutil
import sys
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.catalog_export import (
    EXPORT_SCHEMA_VERSION,
    ExportTargetNotFound,
)
from research_store.blob import ContentAddressedBlobStore
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


def _link_snapshot(config, run_id, url, payload, *, persist_blob=True):
    digest = sha256(payload).hexdigest()
    blob_uri = f"blob://sha256/{digest}"
    if persist_blob:
        ContentAddressedBlobStore(config.blob_root).put(
            BytesIO(payload), "text/plain"
        )
    with connect(config.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO sources(canonical_url,registered_domain)
               VALUES(%s,%s)
               ON CONFLICT(canonical_url) DO UPDATE
                 SET last_seen_at=excluded.last_seen_at
               RETURNING id""",
            (url, "example.test"),
        )
        source_id = cursor.fetchone()[0]
        cursor.execute(
            """INSERT INTO asset_snapshots(
                   source_id,requested_url,final_url,retrieved_at,mime_type,
                   content_sha256,raw_blob_uri,raw_byte_length)
               VALUES(%s,%s,%s,now(),'text/plain',%s,%s,%s)
               RETURNING id""",
            (source_id, url, url, digest, blob_uri, len(payload)),
        )
        snapshot_id = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO research_run_assets(run_id,snapshot_id,role) VALUES(%s,%s,'acquired')",
            (str(run_id), snapshot_id),
        )
    return str(snapshot_id), digest


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
    assert (target_dir / "runs" / f"{result.run['research_run_id']}.json").exists()
    assert (target_dir / "events.jsonl").exists()


def test_export_run_no_state_change(config, run_service, tmp_path):
    """Deleting exports does not change run state."""
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)
    target_dir = tmp_path / "export"

    result = exporter.export_run(run_id, target_dir)
    assert result.status == "complete"

    # Delete export files
    shutil.rmtree(target_dir)

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

    result = exporter.export_run(run_id, blocker)
    assert result.status == "failed"
    assert "exists as a file" in result.error

    with connect(config.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT status,error FROM compatibility_exports WHERE run_id=%s ORDER BY created_at DESC LIMIT 1",
            (str(run_id),),
        )
        export_status, export_error = cursor.fetchone()
    assert export_status == "failed"
    assert "exists as a file" in export_error

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

    import catalog_v5

    previous = os.environ.get("FIRECRAWL_CATALOG_DIR")
    os.environ["FIRECRAWL_CATALOG_DIR"] = str(target_dir)
    try:
        run_data = catalog_v5.read_run(result.run["research_run_id"])
        packet = catalog_v5.build_audit_packet(result.run["research_run_id"])
    finally:
        if previous is None:
            os.environ.pop("FIRECRAWL_CATALOG_DIR", None)
        else:
            os.environ["FIRECRAWL_CATALOG_DIR"] = previous

    assert run_data.get("schema_version") == 5
    assert "research_run_id" in run_data
    assert "objective" in run_data
    assert "lifecycle" in run_data
    assert "invocation_ids" in run_data
    assert packet["target_id"] == result.run["research_run_id"]


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

    inv_file = next((tmp_path / "inv" / "invocations").glob("fc_*.json"))
    import catalog_v5

    previous = os.environ.get("FIRECRAWL_CATALOG_DIR")
    os.environ["FIRECRAWL_CATALOG_DIR"] = str(tmp_path / "inv")
    try:
        inv_data = catalog_v5.read_record(inv_file.stem)
    finally:
        if previous is None:
            os.environ.pop("FIRECRAWL_CATALOG_DIR", None)
        else:
            os.environ["FIRECRAWL_CATALOG_DIR"] = previous

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
        assert event["research_run_id"] == "fr_" + run_id.hex

    import catalog_v5

    previous = os.environ.get("FIRECRAWL_CATALOG_DIR")
    os.environ["FIRECRAWL_CATALOG_DIR"] = str(tmp_path / "events")
    try:
        assert catalog_v5._events_for_run("fr_" + run_id.hex)
    finally:
        if previous is None:
            os.environ.pop("FIRECRAWL_CATALOG_DIR", None)
        else:
            os.environ["FIRECRAWL_CATALOG_DIR"] = previous


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
    run_file = target_dir / "runs" / f"{result.run['research_run_id']}.json"
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
    inv_file = next((tmp_path / "sanitization" / "invocations").glob("fc_*.json"))
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
    """An invalid target is returned and durably recorded as failed."""

    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)

    blocker = tmp_path / "blocker"
    blocker.write_text("blocker")

    result = exporter.export_run(run_id, blocker)
    assert result.status == "failed"
    assert "exists as a file" in result.error


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

    with connect(config.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO semantic_calls(
                   run_id,stage,provider,model,model_revision,prompt_version,
                   input_sha256,status,idempotency_key,request,response_metadata)
               VALUES(%s,'acquisition','local','test-model','rev-1','prompt-v1',
                      %s,'complete',%s,%s,%s)
               RETURNING id""",
            (
                str(run_id), sha256(b"semantic input").hexdigest(),
                f"catalog-export-call:{run_id}",
                json.dumps({"authorization": "Bearer secret-value"}),
                json.dumps({"usage": {"total_tokens": 10}}),
            ),
        )
        call_id = cursor.fetchone()[0]
        cursor.execute(
            "UPDATE audit_assessments SET audit_packet_manifest=%s WHERE id=%s",
            (json.dumps({"semantic_call_ids": [str(call_id)]}), str(assessment_id)),
        )

    result = exporter.export_run(run_id, tmp_path / "failed_stage")

    assert result.status == "complete"
    assert len(result.assessments) == 1

    asmt = result.assessments[0]
    assert asmt["status"] == "partial"  # partial because one stage failed
    assert asmt["stages"]["acquisition"] == {"score": 0.8}
    assert asmt["calls"][0]["model_revision"] == "rev-1"
    assert asmt["calls"][0]["request"]["authorization"] == "[REDACTED]"

    # Verify the failed stage is present
    failed_stages = asmt["stage_errors"]
    assert len(failed_stages) == 1
    assert failed_stages[0]["stage"] == "rubric"
    assert failed_stages[0]["error"] == "rubric evaluation failed"

    import catalog_v5

    root = tmp_path / "failed_stage"
    previous = os.environ.get("FIRECRAWL_CATALOG_DIR")
    os.environ["FIRECRAWL_CATALOG_DIR"] = str(root)
    try:
        stored = catalog_v5.read_path(
            catalog_v5.assessment_path(asmt["target_id"], asmt["assessment_id"])
        )
    finally:
        if previous is None:
            os.environ.pop("FIRECRAWL_CATALOG_DIR", None)
        else:
            os.environ["FIRECRAWL_CATALOG_DIR"] = previous
    assert stored["stages"]["acquisition"] == {"score": 0.8}
    assert stored["stage_errors"][0]["stage"] == "rubric"
    assert isinstance(stored["calls"], list)


def test_regeneration_is_byte_for_byte_stable(config, run_service, tmp_path):
    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)
    first = tmp_path / "first"
    second = tmp_path / "second"

    assert exporter.export_run(run_id, first).status == "complete"
    assert exporter.export_run(run_id, second).status == "complete"

    def tree(root):
        return {
            path.relative_to(root): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    assert tree(first) == tree(second)


def test_events_follow_authoritative_sequence(config, run_service, tmp_path):
    run_id = _create_run_with_data(run_service)
    result = build_catalog_export_service(config).export_events(
        run_id, tmp_path / "events-sequence"
    )
    assert result.status == "complete"
    events = [
        json.loads(line)
        for line in (tmp_path / "events-sequence" / "events.jsonl").read_text().splitlines()
    ]
    assert [item["sequence_number"] for item in events] == sorted(
        item["sequence_number"] for item in events
    )


def test_concurrent_equivalent_exports_publish_complete_tree(
    config, run_service, tmp_path
):
    run_id = _create_run_with_data(run_service)
    target = tmp_path / "concurrent"

    def export_once():
        return build_catalog_export_service(config).export_run(run_id, target)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: export_once(), range(2)))
    assert all(result.status == "complete" for result in results)
    assert (target / "runs" / f"fr_{run_id.hex}.json").is_file()
    assert (target / "manifest.json").is_file()


def test_mid_stage_failure_keeps_previous_export_and_records_failure(
    config, run_service, tmp_path, monkeypatch
):
    import research_store.catalog_export as module

    run_id = _create_run_with_data(run_service)
    exporter = build_catalog_export_service(config)
    target = tmp_path / "atomic"
    assert exporter.export_run(run_id, target).status == "complete"
    before = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    original = module._atomic_write_json

    def fail_manifest(path, payload):
        if path.name == "manifest.json":
            raise module.ExportWriteFailure("injected manifest failure")
        return original(path, payload)

    monkeypatch.setattr(module, "_atomic_write_json", fail_manifest)
    result = exporter.export_run(run_id, target)
    after = {
        path.relative_to(target): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    assert result.status == "failed"
    assert before == after
    with connect(config.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT status FROM compatibility_exports WHERE run_id=%s ORDER BY created_at DESC LIMIT 1",
            (str(run_id),),
        )
        assert cursor.fetchone()[0] == "failed"


def test_snapshots_are_run_scoped_and_blob_backed(config, run_service, tmp_path):
    first_run = _create_run_with_data(run_service)
    second_run = _create_run_with_data(run_service)
    first_snapshot, first_digest = _link_snapshot(
        config, first_run, "https://first.example.test/source", b"first run"
    )
    _link_snapshot(
        config, second_run, "https://second.example.test/source", b"second run"
    )

    result = build_catalog_export_service(config).export_run(
        first_run, tmp_path / "scoped"
    )
    assert result.status == "complete"
    index = json.loads(
        (tmp_path / "scoped" / "snapshots" / "index.json").read_text()
    )
    assert [item["snapshot_id"] for item in index] == [first_snapshot]
    assert (
        tmp_path / "scoped" / "blobs" / "sha256" /
        first_digest[:2] / first_digest
    ).read_bytes() == b"first run"


def test_manifest_uses_authoritative_claim_evidence_provenance(
    config, run_service, tmp_path
):
    run_id = _create_run_with_data(run_service)
    url = "https://evidence.example.test/source"
    snapshot_id, digest = _link_snapshot(config, run_id, url, b"evidence body")
    claim_id = uuid4()
    with connect(config.database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO documents(
                   snapshot_id,title,normalized_text,parser_name,parser_version,
                   normalization_version,document_sha256)
               VALUES(%s,'Evidence','evidence body','plain','v1','v1',%s)
               RETURNING id""",
            (snapshot_id, digest),
        )
        document_id = cursor.fetchone()[0]
        cursor.execute(
            """INSERT INTO chunks(
                   document_id,ordinal,text,content_sha256,chunker_name,chunker_version)
               VALUES(%s,0,'evidence body',%s,'structural','v1')
               RETURNING id""",
            (document_id, digest),
        )
        passage_id = cursor.fetchone()[0]
        cursor.execute(
            """INSERT INTO research_claims(run_id,claim_id,statement)
               VALUES(%s,%s,'Supported claim')""",
            (str(run_id), claim_id),
        )
        cursor.execute(
            """INSERT INTO claim_evidence_links(
                   run_id,claim_id,passage_id,snapshot_id,source_url,
                   relationship,confidence)
               VALUES(%s,%s,%s,%s,%s,'supports',0.9)""",
            (str(run_id), claim_id, passage_id, snapshot_id, url),
        )

    result = build_catalog_export_service(config).export_run(
        run_id, tmp_path / "provenance"
    )
    assert result.status == "complete"
    manifest = json.loads((tmp_path / "provenance" / "manifest.json").read_text())
    assert manifest["sources"] == [{
        "candidate_id": None,
        "canonical_url": "https://evidence.example.test/source",
        "claim_ids": [str(claim_id)],
        "fidelity": "authoritative_passage",
        "passage_ids": [str(passage_id)],
        "relationships": [{
            "claim_id": str(claim_id),
            "confidence": 0.9,
            "passage_id": str(passage_id),
            "passage_sha256": digest,
            "relationship": "supports",
            "snapshot_id": snapshot_id,
        }],
        "resolution": "matched",
        "roles": ["supports"],
        "snapshot_id": snapshot_id,
        "url": "https://evidence.example.test/source",
    }]


def test_missing_blob_fails_without_replacing_catalog(config, run_service, tmp_path):
    run_id = _create_run_with_data(run_service)
    _link_snapshot(
        config, run_id, "https://missing.example.test/source", b"missing", persist_blob=False
    )
    target = tmp_path / "missing-blob"
    target.mkdir()
    (target / "sentinel").write_text("old catalog")

    result = build_catalog_export_service(config).export_run(run_id, target)
    assert result.status == "failed"
    assert "missing or corrupt" in result.error
    assert (target / "sentinel").read_text() == "old catalog"


def test_catalog_export_cli_parity(config, run_service, tmp_path, monkeypatch, capsys):
    from research_store.cli import main

    run_id = _create_run_with_data(run_service)
    with connect(config.database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT external_run_id FROM research_runs WHERE id=%s", (str(run_id),))
        external_run_id = cursor.fetchone()[0]
    monkeypatch.setenv("DATABASE_URL", config.database_url)
    monkeypatch.setenv("BLOB_ROOT", str(config.blob_root))
    target = tmp_path / "cli"
    assert main(["catalog-export", "run", external_run_id, "--target-dir", str(target)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "complete"
    assert (target / "runs" / f"fr_{run_id.hex}.json").is_file()
