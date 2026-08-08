"""Issue #218 production-seam tests for authoritative terminal provenance."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from completion_provenance_test_support import seed_authoritative_completion_provenance
from research_store.config import StoreConfig
from research_store.container import (
    build_run_service,
    build_service,
    build_workflow_operation_service,
)
from research_store.domain import IngestRequest
from research_store.postgres import connect, migrate
from research_store.workflow_service import WorkflowBoundaryError

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


@pytest.fixture
def completion_config(tmp_path: Path) -> StoreConfig:
    migrate(TEST_DSN)
    return replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection=f"completion_{uuid4().hex}",
        embedding_dimension=4,
    )


def _seed_indexing_run(config: StoreConfig):
    runs = build_run_service(config)
    corpus = build_service(config)
    external_id = f"fr_completion_{uuid4().hex}"
    status = runs.create(
        "issue 218 authoritative synthesis provenance",
        external_id,
        execution_mode="autonomous_local",
    )
    manifest = corpus.ingest_batch(
        f"fc_completion_{uuid4().hex}",
        "scrape",
        [
            IngestRequest(
                f"https://completion.example/{uuid4().hex}",
                b"# Completion evidence\n\nPostgreSQL owns authoritative provenance.",
            )
        ],
        research_run_external_id=external_id,
    )
    assert manifest["failure_count"] == 0
    revision = status.lifecycle_revision
    for next_state in (
        "planning",
        "corpus_review",
        "acquiring",
        "extracting",
        "indexing",
    ):
        runs.transition(
            status.id,
            next_state,
            expected_revision=revision,
            idempotency_key=f"completion-seed:{external_id}:{next_state}",
            actor_type="integration-test",
        )
        revision += 1
    current = runs.status(run_id=status.id)
    assert current.state == "indexing"
    return corpus, runs, current


def _mark_run_index_complete(run_id) -> None:
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE index_jobs job
                  SET status='complete', completed_at=now(), error=NULL,
                      lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL
                 FROM embedding_manifests manifest
                 JOIN chunks chunk ON chunk.id=manifest.chunk_id
                 JOIN documents document ON document.id=chunk.document_id
                 JOIN research_run_assets asset
                   ON asset.snapshot_id=document.snapshot_id
                WHERE job.manifest_id=manifest.id AND asset.run_id=%s""",
            (run_id,),
        )
        assert cursor.rowcount > 0
        cursor.execute(
            """UPDATE embedding_manifests manifest
                  SET index_status='complete', indexed_at=now(), error=NULL
                 FROM chunks chunk
                 JOIN documents document ON document.id=chunk.document_id
                 JOIN research_run_assets asset
                   ON asset.snapshot_id=document.snapshot_id
                WHERE manifest.chunk_id=chunk.id AND asset.run_id=%s""",
            (run_id,),
        )


def _ready(config: StoreConfig):
    _corpus, runs, status = _seed_indexing_run(config)
    _mark_run_index_complete(status.id)
    provenance = seed_authoritative_completion_provenance(runs.uow_factory, status.id)
    return runs, status, provenance, build_workflow_operation_service(config)


def test_completed_run_derives_and_persists_exact_authoritative_provenance(
    completion_config: StoreConfig,
):
    _runs, status, provenance, workflow = _ready(completion_config)

    finished = workflow.finish_run(status.external_id, outcome="satisfied")
    assert finished.state == "completed"

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT source_manifest_sha256,answer_sha256
                 FROM research_runs WHERE id=%s""",
            (status.id,),
        )
        assert cursor.fetchone() == (
            provenance.source_manifest_sha256,
            provenance.answer_sha256,
        )
        cursor.execute(
            """SELECT validation_result
                 FROM research_run_transitions
                WHERE run_id=%s AND next_state='completed'""",
            (status.id,),
        )
        completion = cursor.fetchone()[0]["completion"]
        audit = completion["completion_provenance"]
        assert audit["schema_version"] == "completion-provenance-v1"
        assert audit["source_membership_sha256"] == provenance.source_manifest_sha256
        assert audit["semantic_artifact_id"] == str(provenance.draft_artifact_id)
        assert audit["citation_semantic_artifact_id"] == str(
            provenance.citation_artifact_id
        )


def test_caller_hashes_are_optional_assertions_not_authority(
    completion_config: StoreConfig,
):
    _runs, status, provenance, workflow = _ready(completion_config)
    finished = workflow.finish_run(
        status.external_id,
        outcome="satisfied",
        source_manifest_sha256=provenance.source_manifest_sha256,
        answer_sha256=provenance.answer_sha256,
    )
    assert finished.state == "completed"


@pytest.mark.parametrize(
    ("field", "value", "pattern"),
    [
        ("source_manifest_sha256", "x", "64 hexadecimal"),
        ("answer_sha256", "not-a-sha", "64 hexadecimal"),
        ("source_manifest_sha256", "f" * 64, "active sealed membership"),
        ("answer_sha256", "e" * 64, "immutable synthesis artifact"),
    ],
)
def test_completion_rejects_malformed_or_mismatched_digest_assertions(
    completion_config: StoreConfig, field: str, value: str, pattern: str
):
    _runs, status, _provenance, workflow = _ready(completion_config)
    kwargs = {field: value}
    with pytest.raises(WorkflowBoundaryError, match=pattern):
        workflow.finish_run(status.external_id, outcome="satisfied", **kwargs)


@pytest.mark.parametrize(
    ("column", "pattern"),
    [
        ("semantic_call_id", "semantic call"),
        ("semantic_artifact_id", "immutable artifact"),
    ],
)
def test_completion_rejects_missing_immutable_synthesis_provenance(
    completion_config: StoreConfig, column: str, pattern: str
):
    _runs, status, _provenance, workflow = _ready(completion_config)
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE synthesis_stages SET {column}=NULL "
            "WHERE run_id=%s AND stage_name='draft'",
            (status.id,),
        )
    with pytest.raises(WorkflowBoundaryError, match=pattern):
        workflow.finish_run(status.external_id, outcome="satisfied")


def test_completion_rejects_external_authority_even_when_label_is_omitted(
    completion_config: StoreConfig,
):
    _runs, status, _provenance, workflow = _ready(completion_config)
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE semantic_calls c
                  SET provider='host-agent'
                 FROM synthesis_stages s
                WHERE s.run_id=%s AND s.stage_name='draft'
                  AND c.id=s.semantic_call_id""",
            (status.id,),
        )
    with pytest.raises(WorkflowBoundaryError, match="not authoritative"):
        workflow.finish_run(status.external_id, outcome="satisfied")


def test_completion_rejects_stale_validation_after_new_evidence_packet(
    completion_config: StoreConfig,
):
    _runs, status, provenance, workflow = _ready(completion_config)
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT payload,research_spec_id,coverage_revision FROM evidence_packets "
            "WHERE run_id=%s AND packet_revision=%s",
            (status.id, provenance.evidence_packet_revision),
        )
        payload, spec_id, coverage_revision = cursor.fetchone()
        cursor.execute(
            """INSERT INTO evidence_packets(
                   id,run_id,research_spec_id,coverage_revision,packet_revision,payload)
                 VALUES(%s,%s,%s,%s,%s,%s::jsonb)""",
            (
                uuid4(),
                status.id,
                spec_id,
                coverage_revision,
                provenance.evidence_packet_revision + 1,
                json.dumps(payload),
            ),
        )
    with pytest.raises(WorkflowBoundaryError, match="stale"):
        workflow.finish_run(status.external_id, outcome="satisfied")


def test_completion_rejects_valid_but_incomplete_validation(
    completion_config: StoreConfig,
):
    _runs, status, _provenance, workflow = _ready(completion_config)
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE synthesis_stages
                  SET artifact=jsonb_set(
                        jsonb_set(artifact,'{is_complete}','false'::jsonb),
                        '{validation_warnings_count}','1'::jsonb)
                WHERE run_id=%s AND stage_name='validation'""",
            (status.id,),
        )
    with pytest.raises(WorkflowBoundaryError, match="incomplete"):
        workflow.finish_run(status.external_id, outcome="satisfied")


def test_completion_rejects_packet_without_required_evidence_links(
    completion_config: StoreConfig,
):
    _runs, status, provenance, workflow = _ready(completion_config)
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT payload FROM evidence_packets "
            "WHERE run_id=%s AND packet_revision=%s",
            (status.id, provenance.evidence_packet_revision),
        )
        payload = cursor.fetchone()[0]
        payload["claim_evidence_bindings"] = []
        cursor.execute(
            "UPDATE evidence_packets SET payload=%s::jsonb "
            "WHERE run_id=%s AND packet_revision=%s",
            (json.dumps(payload), status.id, provenance.evidence_packet_revision),
        )
    with pytest.raises(WorkflowBoundaryError, match="evidence links"):
        workflow.finish_run(status.external_id, outcome="satisfied")


def test_terminal_transaction_revalidates_preflight_provenance(
    completion_config: StoreConfig,
    monkeypatch,
):
    _runs, status, _provenance, workflow = _ready(completion_config)
    original_transition = workflow._transition
    mutated = False

    def transition_with_mutation(current, next_state, **kwargs):
        nonlocal mutated
        if next_state == "completed" and not mutated:
            mutated = True
            with connect(TEST_DSN) as connection, connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE synthesis_stages
                          SET evidence_packet_revision=evidence_packet_revision+1
                        WHERE run_id=%s AND stage_name='validation'""",
                    (status.id,),
                )
        return original_transition(current, next_state, **kwargs)

    monkeypatch.setattr(workflow, "_transition", transition_with_mutation)
    with pytest.raises(Exception, match="revalidation|provenance changed|stale"):
        workflow.finish_run(status.external_id, outcome="satisfied")


def test_completion_rejects_evidence_packet_membership_drift(
    completion_config: StoreConfig,
):
    _runs, status, provenance, workflow = _ready(completion_config)
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT payload FROM evidence_packets "
            "WHERE run_id=%s AND packet_revision=%s",
            (status.id, provenance.evidence_packet_revision),
        )
        payload = cursor.fetchone()[0]
        payload["passages"].append(
            {
                "passage_id": str(uuid4()),
                "snapshot_id": str(uuid4()),
                "text": "out-of-membership evidence",
                "source_url": "https://outside.example/",
            }
        )
        cursor.execute(
            "UPDATE evidence_packets SET payload=%s::jsonb "
            "WHERE run_id=%s AND packet_revision=%s",
            (json.dumps(payload), status.id, provenance.evidence_packet_revision),
        )
    with pytest.raises(WorkflowBoundaryError, match="sealed source membership"):
        workflow.finish_run(status.external_id, outcome="satisfied")


def test_completion_fails_closed_when_provenance_loader_errors(
    completion_config: StoreConfig,
    monkeypatch,
):
    _runs, status, _provenance, workflow = _ready(completion_config)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("injected provenance-store outage")

    monkeypatch.setattr(
        "research_store.workflow_service.load_authoritative_completion_provenance",
        unavailable,
    )
    with pytest.raises(WorkflowBoundaryError, match=r"fails closed \(RuntimeError\)"):
        workflow.finish_run(status.external_id, outcome="satisfied")


def test_failed_outcome_never_infers_authoritative_synthesis(
    completion_config: StoreConfig,
):
    _corpus, runs, status = _seed_indexing_run(completion_config)
    workflow = build_workflow_operation_service(completion_config)
    failed = workflow.finish_run(
        status.external_id,
        outcome="infrastructure lag",
        status_name="failed",
    )
    assert failed.state == "failed"
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT source_manifest_sha256,answer_sha256
                 FROM research_runs WHERE id=%s""",
            (status.id,),
        )
        assert cursor.fetchone() == (None, None)
        cursor.execute(
            "SELECT count(*) FROM synthesis_stages WHERE run_id=%s",
            (status.id,),
        )
        assert cursor.fetchone()[0] == 0
    assert runs.status(run_id=status.id).state == "failed"
