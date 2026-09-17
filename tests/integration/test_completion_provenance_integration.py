"""Issue #218 production-seam tests for authoritative terminal provenance."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from completion_provenance_test_support import seed_authoritative_completion_provenance
from psycopg import sql

from firecrawl_skill.research_store.composition import (
    build_run_service,
    build_service,
    build_workflow_operation_service,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.domain import IngestRequest
from firecrawl_skill.research_store.postgres import connect, migrate
from firecrawl_skill.research_store.workflow_service import WorkflowBoundaryError

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
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


def _seed_indexing_run(
    config: StoreConfig, *, execution_mode: str = "autonomous_local"
):
    runs = build_run_service(config)
    corpus = build_service(config)
    external_id = f"fr_completion_{uuid4().hex}"
    status = runs.create(
        "issue 218 authoritative synthesis provenance",
        external_id,
        execution_mode=execution_mode,
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
    workflow = build_workflow_operation_service(config)
    assert status.external_id is not None
    workflow._finalize_indexing(
        status.external_id,
        f"completion-test:{status.id}:finalize-indexing",
    )
    status = runs.status(run_id=status.id)
    assert status.state == "coverage_review"
    provenance = seed_authoritative_completion_provenance(runs.uow_factory, status.id)
    return runs, status, provenance, workflow


def test_completed_run_derives_and_persists_exact_authoritative_provenance(
    completion_config: StoreConfig,
):
    _runs, status, provenance, workflow = _ready(completion_config)

    assert status.external_id is not None
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
        completion_row = cursor.fetchone()
        assert completion_row is not None
        completion = completion_row[0]["completion"]
        audit = completion["completion_provenance"]
        assert audit["schema_version"] == "completion-provenance-v1"
        assert audit["source_membership_sha256"] == provenance.source_manifest_sha256
        assert audit["semantic_artifact_id"] == str(provenance.draft_artifact_id)
        assert audit["citation_semantic_artifact_id"] == str(
            provenance.citation_artifact_id
        )
        assert audit["coverage_revision"] == provenance.coverage_revision
        assert audit["coverage_snapshot_sha256"] == provenance.coverage_snapshot_sha256
        cursor.execute(
            """SELECT coverage_revision FROM terminal_decisions
                 WHERE run_id=%s AND outcome='sufficient'
                 ORDER BY created_at DESC LIMIT 1""",
            (status.id,),
        )
        assert cursor.fetchone() == (provenance.coverage_revision,)


def test_completion_rejects_later_sufficient_projection_when_packet_snapshot_is_insufficient(
    completion_config: StoreConfig,
):
    _runs, status, provenance, workflow = _ready(completion_config)
    insufficient_revision = provenance.coverage_revision + 1
    later_sufficient_revision = provenance.coverage_revision + 2

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT ledger FROM coverage_snapshots
                 WHERE run_id=%s AND coverage_revision=%s""",
            (status.id, provenance.coverage_revision),
        )
        row = cursor.fetchone()
        assert row is not None
        sufficient_ledger = json.loads(json.dumps(row[0]))

        insufficient_ledger = json.loads(json.dumps(sufficient_ledger))
        insufficient_ledger["revision"] = insufficient_revision
        insufficient_ledger["overall_status"] = "insufficient"
        for item in insufficient_ledger["items"]:
            item["status"] = "unassessed"
            item["remaining_gap"] = "coverage was not yet terminal-grade"
        insufficient_hash = hashlib.sha256(
            json.dumps(
                insufficient_ledger,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        cursor.execute(
            """INSERT INTO coverage_snapshots(
                   run_id,coverage_revision,ledger,content_sha256)
                 VALUES(%s,%s,%s::jsonb,%s)""",
            (
                status.id,
                insufficient_revision,
                json.dumps(insufficient_ledger),
                insufficient_hash,
            ),
        )

        later_sufficient = json.loads(json.dumps(sufficient_ledger))
        later_sufficient["revision"] = later_sufficient_revision
        later_sufficient_hash = hashlib.sha256(
            json.dumps(
                later_sufficient,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        cursor.execute(
            """INSERT INTO coverage_snapshots(
                   run_id,coverage_revision,ledger,content_sha256)
                 VALUES(%s,%s,%s::jsonb,%s)""",
            (
                status.id,
                later_sufficient_revision,
                json.dumps(later_sufficient),
                later_sufficient_hash,
            ),
        )
        cursor.execute(
            """SELECT payload FROM evidence_packets
                 WHERE run_id=%s AND packet_revision=%s""",
            (status.id, provenance.evidence_packet_revision),
        )
        packet_row = cursor.fetchone()
        assert packet_row is not None
        packet_payload = dict(packet_row[0])
        packet_payload["coverage_revision"] = insufficient_revision
        cursor.execute(
            """UPDATE evidence_packets
                  SET coverage_revision=%s,payload=%s::jsonb
                WHERE run_id=%s AND packet_revision=%s""",
            (
                insufficient_revision,
                json.dumps(packet_payload),
                status.id,
                provenance.evidence_packet_revision,
            ),
        )
        cursor.execute(
            "UPDATE research_runs SET current_coverage_revision=%s WHERE id=%s",
            (later_sufficient_revision, status.id),
        )

    with pytest.raises(
        WorkflowBoundaryError,
        match="EvidencePacket-bound coverage snapshot to be sufficient",
    ):
        assert status.external_id is not None
        workflow.finish_run(status.external_id, outcome="satisfied")


def test_completion_rejects_packet_payload_coverage_revision_that_disagrees_with_row(
    completion_config: StoreConfig,
):
    runs, status, provenance, workflow = _ready(completion_config)
    contradictory_revision = provenance.coverage_revision + 1

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT ledger FROM coverage_snapshots
                 WHERE run_id=%s AND coverage_revision=%s""",
            (status.id, provenance.coverage_revision),
        )
        row = cursor.fetchone()
        assert row is not None
        insufficient_ledger = json.loads(json.dumps(row[0]))
        insufficient_ledger["revision"] = contradictory_revision
        insufficient_ledger["overall_status"] = "insufficient"
        for item in insufficient_ledger["items"]:
            item["status"] = "unassessed"
            item["remaining_gap"] = "coverage was not yet terminal-grade"
        insufficient_hash = hashlib.sha256(
            json.dumps(
                insufficient_ledger,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        cursor.execute(
            """INSERT INTO coverage_snapshots(
                   run_id,coverage_revision,ledger,content_sha256)
                 VALUES(%s,%s,%s::jsonb,%s)""",
            (
                status.id,
                contradictory_revision,
                json.dumps(insufficient_ledger),
                insufficient_hash,
            ),
        )
        cursor.execute(
            """SELECT payload FROM evidence_packets
                 WHERE run_id=%s AND packet_revision=%s""",
            (status.id, provenance.evidence_packet_revision),
        )
        packet_row = cursor.fetchone()
        assert packet_row is not None
        packet_payload = dict(packet_row[0])
        packet_payload["coverage_revision"] = contradictory_revision
        cursor.execute(
            """UPDATE evidence_packets
                  SET payload=%s::jsonb
                WHERE run_id=%s AND packet_revision=%s""",
            (
                json.dumps(packet_payload),
                status.id,
                provenance.evidence_packet_revision,
            ),
        )

    with pytest.raises(
        WorkflowBoundaryError,
        match="coverage revision contradicts persisted packet authority",
    ):
        assert status.external_id is not None
        workflow.finish_run(status.external_id, outcome="satisfied")
    assert runs.status(run_id=status.id).state != "completed"


def test_caller_hashes_are_optional_assertions_not_authority(
    completion_config: StoreConfig,
):
    _runs, status, provenance, workflow = _ready(completion_config)
    assert status.external_id is not None
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
        assert status.external_id is not None
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
            sql.SQL(
                "UPDATE synthesis_stages SET {} = NULL "
                "WHERE run_id=%s AND stage_name='draft'"
            ).format(sql.Identifier(column)),
            (status.id,),
        )
    with pytest.raises(WorkflowBoundaryError, match=pattern):
        assert status.external_id is not None
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
        assert status.external_id is not None
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
        row = cursor.fetchone()
        assert row is not None
        payload, spec_id, coverage_revision = row
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
    with pytest.raises(
        WorkflowBoundaryError,
        match="EvidencePacket claim does not match current persisted claim provenance",
    ):
        assert status.external_id is not None
        workflow.finish_run(status.external_id, outcome="satisfied")


def test_resume_recovers_stale_completed_stages_after_packet_advance_crash(
    completion_config: StoreConfig,
    monkeypatch,
):
    """A fresh process rebuilds every stale stage before terminal provenance."""
    from completion_provenance_test_support import seed_completion_prerequisites

    from firecrawl_skill.research_store.assessment.evidence import EvidenceService
    from firecrawl_skill.research_store.budget_policy import DEFAULT_POLICY
    from firecrawl_skill.research_store.completion_provenance import (
        load_authoritative_completion_provenance,
    )
    from firecrawl_skill.research_store.reporting.construction import (
        LocalSynthesisService,
    )
    from firecrawl_skill.research_store.semantic_service import SemanticCallService

    monkeypatch.setenv("FIRECRAWL_RELEASE_DETERMINISTIC_FIXTURES", "1")
    _corpus, runs, status = _seed_indexing_run(
        completion_config,
        execution_mode="deterministic_debug",
    )
    _mark_run_index_complete(status.id)
    workflow = build_workflow_operation_service(completion_config)
    assert status.external_id is not None
    workflow._finalize_indexing(
        status.external_id,
        f"completion-test:{status.id}:packet-crash-finalize-indexing",
    )
    status = runs.status(run_id=status.id)
    assert status.state == "coverage_review"

    original = seed_authoritative_completion_provenance(runs.uow_factory, status.id)
    advanced = seed_completion_prerequisites(runs.uow_factory, status.id)
    active_revision = int(advanced["packet_revision"])
    assert active_revision == original.evidence_packet_revision + 1

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT stage_name,stage_status,evidence_packet_revision
                 FROM synthesis_stages
                WHERE run_id=%s ORDER BY stage_name""",
            (status.id,),
        )
        crashed_rows = cursor.fetchall()
    assert len(crashed_rows) == 5
    assert all(row[1] == "completed" for row in crashed_rows)
    assert all(row[2] == original.evidence_packet_revision for row in crashed_rows)

    semantic = SemanticCallService(runs.uow_factory)
    evidence = EvidenceService(runs.uow_factory, budget_policy=DEFAULT_POLICY)
    service = LocalSynthesisService(
        semantic_service=semantic,
        evidence_service=evidence,
        config=completion_config,
    )
    summary = service.run_synthesis(
        run_id=status.id,
        packet_revision=active_revision,
        model_name="",
    )
    assert summary["overall_status"] == "completed"

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT stage_name,stage_status,evidence_packet_revision
                 FROM synthesis_stages
                WHERE run_id=%s ORDER BY stage_name""",
            (status.id,),
        )
        recovered_rows = cursor.fetchall()
    assert len(recovered_rows) == 5
    assert all(row[1] == "completed" for row in recovered_rows)
    assert all(row[2] == active_revision for row in recovered_rows)

    with runs.uow_factory() as uow:
        provenance = load_authoritative_completion_provenance(uow, status.id)
    assert provenance.evidence_packet_revision == active_revision


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
        assert status.external_id is not None
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
        payload = cursor.fetchone()
        assert payload is not None
        payload = payload[0]
        payload["claim_evidence_bindings"] = []
        cursor.execute(
            "UPDATE evidence_packets SET payload=%s::jsonb "
            "WHERE run_id=%s AND packet_revision=%s",
            (json.dumps(payload), status.id, provenance.evidence_packet_revision),
        )
    with pytest.raises(WorkflowBoundaryError, match="evidence links"):
        assert status.external_id is not None
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
        assert status.external_id is not None
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
        payload = cursor.fetchone()
        assert payload is not None
        payload = payload[0]
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
        assert status.external_id is not None
        workflow.finish_run(status.external_id, outcome="satisfied")


def test_completion_fails_closed_when_provenance_loader_errors(
    completion_config: StoreConfig,
    monkeypatch,
):
    _runs, status, _provenance, workflow = _ready(completion_config)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("injected provenance-store outage")

    monkeypatch.setattr(
        "firecrawl_skill.research_store.workflow_service.load_authoritative_completion_provenance",
        unavailable,
    )
    with pytest.raises(WorkflowBoundaryError, match=r"fails closed \(RuntimeError\)"):
        assert status.external_id is not None
        workflow.finish_run(status.external_id, outcome="satisfied")


def test_failed_outcome_never_infers_authoritative_synthesis(
    completion_config: StoreConfig,
):
    _corpus, runs, status = _seed_indexing_run(completion_config)
    workflow = build_workflow_operation_service(completion_config)
    assert status.external_id is not None
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
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == 0
    assert runs.status(run_id=status.id).state == "failed"
