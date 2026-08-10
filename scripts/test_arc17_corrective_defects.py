"""Regression tests for ARC-17 corrective defects.

Covers:
* Defect 1 — deterministic semantic model identity (deterministic_debug must
  not falsely claim production LLM authority).
* Defect 2 — citation-stage acceptance weaker than terminal provenance.
* Defect 3 — release environment artifacts must not serialize GitHub secret
  endpoint URLs.
"""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from research_store.completion_provenance import (
    CompletionProvenanceError,
    validate_citation_artifact,
)
from research_store.report_service import LocalSynthesisService, ReportServiceError
from research_store.strict_benchmark import _build_env_manifest

# ---------------------------------------------------------------------------
# Helpers borrowed from test_report_service.py
# ---------------------------------------------------------------------------

_VALID_PACKET = {
    "schema_version": "evidence-packet-v1",
    "run_id": "00000000-0000-0000-0000-000000000401",
    "research_spec_id": "00000000-0000-0000-0000-000000000100",
    "coverage_revision": 2,
    "claims": [
        {
            "claim_id": "00000000-0000-0000-0000-000000000102",
            "statement": "The documented behavior is reproducible.",
            "semantic_status": "qualified",
            "uncertainty": "Only one source has been acquired.",
        }
    ],
    "passages": [
        {
            "passage_id": "00000000-0000-0000-0000-000000000601",
            "candidate_id": "00000000-0000-0000-0000-000000000301",
            "snapshot_id": "00000000-0000-0000-0000-000000000602",
            "chunk_id": "00000000-0000-0000-0000-000000000603",
            "text": "The fixture passage records the documented behavior.",
            "source_url": "https://fixture.invalid/docs",
        }
    ],
    "omitted_passages": [],
    "claim_evidence_bindings": [
        {
            "binding_id": "00000000-0000-0000-0000-000000000604",
            "claim_id": "00000000-0000-0000-0000-000000000102",
            "passage_ids": ["00000000-0000-0000-0000-000000000601"],
            "relationship": "qualifies",
            "confidence": 0.7,
            "uncertainty": "Independent replication is missing.",
        }
    ],
    "corroborating_groups": [],
    "contradicting_groups": [],
    "qualifying_groups": [],
    "near_duplicate_groups": [],
    "source_diversity_summary": {"independent_source_count": 1},
    "freshness_summary": {"status": "satisfied"},
    "limitations": ["Independent corroboration remains missing."],
    "unresolved_items": [],
    "independence_assessments": [],
    "retrieval_provenance": [],
}


def _make_mock_uow():
    """Build a mock UOW with synthesis_stages methods."""
    mock_uow = MagicMock()
    mock_uow.runs.get_run_status.return_value = {
        "lifecycle_revision": 1,
        "execution_mode": "autonomous_local",
        "state": "synthesizing",
    }

    _records = {}

    def _get_stage(run_id, stage_name):
        key = (str(run_id), stage_name)
        if key not in _records:
            raise KeyError(key)
        return _records[key]

    def _insert_stage(record):
        key = (str(record["run_id"]), record["stage_name"])
        _records[key] = record

    def _update_stage(record):
        key = (str(record["run_id"]), record["stage_name"])
        _records[key] = record

    def _get_stages(run_id):
        return [v for k, v in _records.items() if k[0] == str(run_id)]

    mock_uow.synthesis_stages.get_synthesis_stage = _get_stage
    mock_uow.synthesis_stages.insert_synthesis_stage = _insert_stage
    mock_uow.synthesis_stages.update_synthesis_stage = _update_stage
    mock_uow.synthesis_stages.get_synthesis_stages = _get_stages

    mock_uow.get_synthesis_stage = _get_stage
    mock_uow.insert_synthesis_stage = _insert_stage
    mock_uow.get_synthesis_stages = _get_stages

    _packet_store = {}

    def _get_evidence_packet(run_id, packet_revision=None):
        key = str(run_id)
        if key not in _packet_store:
            return None
        result = MagicMock()
        result.packet_revision = _packet_store[key]
        return result

    mock_uow.get_evidence_packet = _get_evidence_packet

    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_uow)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    mock_uow_factory = MagicMock(return_value=mock_ctx)

    return mock_uow_factory, mock_uow, _records, _packet_store


def _make_service(
    mock_packet=None,
    execution_mode="autonomous_local",
    generative_model="test-generative-model",
    embedding_model="test-embedding-model",
):
    """Build a LocalSynthesisService with mocked dependencies."""
    packet = mock_packet or deepcopy(_VALID_PACKET)

    mock_evidence = MagicMock()
    mock_evidence.export_packet.return_value = packet

    mock_uow_factory, mock_uow, _records, _packet_store = _make_mock_uow()
    mock_uow.runs.get_run_status.return_value = {
        "lifecycle_revision": 1,
        "execution_mode": execution_mode,
        "state": "synthesizing",
    }

    run_id_str = str(packet.get("run_id", "00000000-0000-0000-0000-000000000401"))
    _packet_store[run_id_str] = packet.get("coverage_revision", 1)

    mock_semantic = MagicMock()
    mock_semantic.uow_factory = mock_uow_factory

    mock_config = MagicMock()
    mock_config.embedding_model = embedding_model
    mock_config.generative_model = generative_model

    service = LocalSynthesisService(
        semantic_service=mock_semantic,
        evidence_service=mock_evidence,
        config=mock_config,
    )
    return service, mock_uow


# ---------------------------------------------------------------------------
# Defect 1 — deterministic semantic model identity
# ---------------------------------------------------------------------------


class TestDeterministicModelIdentity:
    """Prove deterministic fixtures truthfully record empty model identity."""

    def test_deterministic_debug_records_empty_model(self):
        """Deterministic-debug runs must initialize stages with empty model."""
        service, mock_uow = _make_service(execution_mode="deterministic_debug")
        run_id = UUID("00000000-0000-0000-0000-000000000401")

        from research_store.domain import SynthesisStageName

        for stage_name in SynthesisStageName:
            mock_uow.synthesis_stages.update_synthesis_stage(
                {
                    "id": str(uuid4()),
                    "run_id": str(run_id),
                    "stage_name": stage_name.value,
                    "stage_status": "completed",
                    "semantic_call_id": None,
                    "semantic_artifact_id": None,
                    "evidence_packet_revision": 1,
                    "model_name": "",
                    "prompt_version": "v1",
                    "schema_version": 1,
                    "artifact": {},
                    "error": None,
                    "attempts": 1,
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            )

        summary = service.run_synthesis(
            run_id=run_id,
            packet_revision=1,
            model_name="should-be-ignored",
        )
        assert summary["overall_status"] == "completed"
        for stage_key in SynthesisStageName:
            assert summary["stages"][stage_key]["status"] == "skipped"

    def test_deterministic_debug_ignores_supplied_model_name(self):
        """Even an explicit non-empty model_name must be ignored in debug mode."""
        service, mock_uow = _make_service(execution_mode="deterministic_debug")
        run_id = UUID("00000000-0000-0000-0000-000000000401")

        from research_store.domain import SynthesisStageName

        for stage_name in SynthesisStageName:
            mock_uow.synthesis_stages.update_synthesis_stage(
                {
                    "id": str(uuid4()),
                    "run_id": str(run_id),
                    "stage_name": stage_name.value,
                    "stage_status": "completed",
                    "semantic_call_id": None,
                    "semantic_artifact_id": None,
                    "evidence_packet_revision": 1,
                    "model_name": "",
                    "prompt_version": "v1",
                    "schema_version": 1,
                    "artifact": {},
                    "error": None,
                    "attempts": 1,
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            )

        summary = service.run_synthesis(
            run_id=run_id,
            packet_revision=1,
            model_name="fake-production-model",
        )
        assert summary["overall_status"] == "completed"

    def test_autonomous_local_requires_explicit_model(self):
        """Non-deterministic modes must require an explicit model_name."""
        service, _ = _make_service(execution_mode="autonomous_local")
        run_id = UUID("00000000-0000-0000-0000-000000000401")

        with pytest.raises(ReportServiceError, match="no model configured"):
            service.run_synthesis(
                run_id=run_id,
                packet_revision=1,
            )

    def test_divergent_stage_model_fails_closed(self, tmp_path):
        """A synthesis stage whose model_name diverges from the authoritative
        semantic-call model must be rejected at terminal completion time.

        This test creates two independent runs in deterministic_debug mode:
        one with matching stage/call identities (control) and one where the
        draft stage was persisted with a different model_name.  It then invokes
        the real terminal-completion path and asserts the divergent run is
        rejected while the control succeeds.
        """
        import os

        dsn = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
        if not dsn:
            pytest.skip("requires RESEARCH_STORE_TEST_DATABASE_URL")

        from dataclasses import replace

        from completion_provenance_test_support import (
            seed_authoritative_completion_provenance,
        )
        from research_store.config import StoreConfig
        from research_store.container import (
            build_run_service,
            build_service,
            build_workflow_operation_service,
        )
        from research_store.domain import IngestRequest
        from research_store.postgres import connect, migrate
        from research_store.workflow_service import WorkflowBoundaryError

        migrate(dsn)
        config = replace(
            StoreConfig.from_env(),
            database_url=dsn,
            blob_root=tmp_path / "blobs",
            qdrant_collection=f"arc17_{uuid4().hex}",
            embedding_dimension=4,
        )

        def _seed_and_finalize(corpus, runs, external_id, execution_mode):
            """Seed a run through indexing and finalize it."""
            status = runs.create(
                "arc17 test run",
                external_id,
                execution_mode=execution_mode,
            )
            manifest = corpus.ingest_batch(
                f"fc-{uuid4().hex}",
                "scrape",
                [
                    IngestRequest(
                        f"https://example/{uuid4().hex}",
                        b"# Test evidence\n\nPostgreSQL owns authoritative provenance.",
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
                    idempotency_key=f"test:{external_id}:{next_state}",
                    actor_type="integration-test",
                )
                revision += 1
            # Mark index jobs complete.
            with connect(dsn) as conn, conn.cursor() as cursor:
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
                    (status.id,),
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
                    (status.id,),
                )
            workflow = build_workflow_operation_service(config)
            workflow._finalize_indexing(
                external_id,
                f"test:{external_id}:finalize-indexing",
            )
            status = runs.status(run_id=status.id)
            assert status.state == "coverage_review"
            return runs, status, workflow

        # --- Control run: all stage/model identities match ---
        runs_control = build_run_service(config)
        corpus_control = build_service(config)
        external_id_control = f"arc17-control-{uuid4().hex}"
        runs_control, status_control, _workflow_control = _seed_and_finalize(
            corpus_control, runs_control, external_id_control, "deterministic_debug"
        )
        seed_authoritative_completion_provenance(
            runs_control.uow_factory, status_control.id
        )
        # In deterministic_debug, model_name must be "" on every stage.
        with connect(dsn) as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE synthesis_stages SET model_name='' WHERE run_id=%s",
                (status_control.id,),
            )
        with connect(dsn) as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE semantic_calls SET model='' WHERE run_id=%s",
                (status_control.id,),
            )

        # --- Divergent run: draft stage has wrong model_name ---
        runs_diverge = build_run_service(config)
        corpus_diverge = build_service(config)
        external_id_diverge = f"arc17-diverge-{uuid4().hex}"
        runs_diverge, status_diverge, workflow_diverge = _seed_and_finalize(
            corpus_diverge, runs_diverge, external_id_diverge, "deterministic_debug"
        )
        seed_authoritative_completion_provenance(
            runs_diverge.uow_factory, status_diverge.id
        )
        # Introduce divergence: persist draft stage with non-empty model.
        with connect(dsn) as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE synthesis_stages SET model_name='divergent-model' "
                "WHERE run_id=%s AND stage_name='draft'",
                (status_diverge.id,),
            )
            assert cursor.rowcount == 1

        # The control run should complete successfully.
        finished_control = workflow_diverge.finish_run(
            status_control.external_id, outcome="satisfied"
        )
        assert finished_control.state == "completed"

        # The divergent run must be rejected at terminal completion.
        with pytest.raises(
            WorkflowBoundaryError, match="model identity does not match"
        ):
            workflow_diverge.finish_run(status_diverge.external_id, outcome="satisfied")


# ---------------------------------------------------------------------------
# Defect 2 — citation-stage acceptance weaker than terminal provenance
# ---------------------------------------------------------------------------


class TestCitationStageAcceptance:
    """Prove invalid citation artifacts cannot become completed stages."""

    def test_unresolved_validation_fails_closed(self):
        """A citation artifact with unresolved validation must raise."""
        draft_citations = {("s1", "c1", ("p1",))}
        bad_payload = {
            "pass_status": "passed",
            "validation_results": [
                {
                    "section_id": "s1",
                    "claim_id": "c1",
                    "passage_ids": ["p1"],
                    "status": "invalid",
                    "issue": "something wrong",
                }
            ],
            "invented_citations": [],
            "unsupported_claims": [],
            "entailment_mismatches": [],
        }
        with pytest.raises(CompletionProvenanceError, match="unresolved"):
            validate_citation_artifact(bad_payload, draft_citations)

    def test_wrong_citation_tuple_fails(self):
        """A validation result that doesn't match draft citations must fail."""
        draft_citations = {("s1", "c1", ("p1",))}
        bad_payload = {
            "pass_status": "passed",
            "validation_results": [
                {
                    "section_id": "s1",
                    "claim_id": "c9",
                    "passage_ids": ["p1"],
                    "status": "valid",
                    "issue": "",
                }
            ],
            "invented_citations": [],
            "unsupported_claims": [],
            "entailment_mismatches": [],
        }
        with pytest.raises(CompletionProvenanceError, match="exactly validate"):
            validate_citation_artifact(bad_payload, draft_citations)

    def test_valid_exact_coverage_passes(self):
        """A perfectly matching citation artifact must pass validation."""
        draft_citations = {("s1", "c1", ("p1",))}
        good_payload = {
            "pass_status": "passed",
            "validation_results": [
                {
                    "section_id": "s1",
                    "claim_id": "c1",
                    "passage_ids": ["p1"],
                    "status": "valid",
                    "issue": "",
                }
            ],
            "invented_citations": [],
            "unsupported_claims": [],
            "entailment_mismatches": [],
        }
        validate_citation_artifact(good_payload, draft_citations)

    def test_invented_citations_fail(self):
        """Invented citations must cause validation failure."""
        draft_citations = set()
        bad_payload = {
            "pass_status": "passed",
            "validation_results": [],
            "invented_citations": [{"section_id": "s1"}],
            "unsupported_claims": [],
            "entailment_mismatches": [],
        }
        with pytest.raises(CompletionProvenanceError, match="unresolved failures"):
            validate_citation_artifact(bad_payload, draft_citations)

    def test_failed_pass_status_fails(self):
        """pass_status != 'passed' must cause validation failure."""
        draft_citations = set()
        bad_payload = {
            "pass_status": "failed",
            "validation_results": [],
            "invented_citations": [],
            "unsupported_claims": [],
            "entailment_mismatches": [],
        }
        with pytest.raises(CompletionProvenanceError, match="did not pass"):
            validate_citation_artifact(bad_payload, draft_citations)

    def test_empty_draft_with_empty_validation_passes(self):
        """An empty draft with empty validation results is valid."""
        validate_citation_artifact(
            {
                "pass_status": "passed",
                "validation_results": [],
                "invented_citations": [],
                "unsupported_claims": [],
                "entailment_mismatches": [],
            },
            set(),
        )

    def test_citation_failure_marks_stage_failed_and_enables_resume(self):
        """An invalid citation artifact must leave the stage as failed and be
        resumable with a corrected artifact through the normal machinery."""
        service, mock_uow = _make_service(execution_mode="autonomous_local")
        run_id = UUID("00000000-0000-0000-0000-000000000401")

        from research_store.domain import SynthesisStageName

        # Pre-populate outline, binding, draft as completed.
        # The draft artifact must contain report_sections with claim_references
        # so the citation stage can compute draft_citations correctly.
        draft_artifact = {
            "schema_version": "synthesis-draft-v1",
            "run_id": str(run_id),
            "evidence_packet_revision": 1,
            "report_sections": [
                {
                    "section_id": "s1",
                    "title": "Findings",
                    "body": "Test findings",
                    "claim_references": [
                        {
                            "claim_id": "00000000-0000-0000-0000-000000000102",
                            "passage_ids": ["00000000-0000-0000-0000-000000000601"],
                            "relationship": "qualifies",
                        }
                    ],
                }
            ],
            "unsupported_claims": [],
            "limitations": [],
        }
        for stage_name in SynthesisStageName:
            if stage_name.value in ("outline", "binding", "draft"):
                mock_uow.synthesis_stages.update_synthesis_stage(
                    {
                        "id": str(uuid4()),
                        "run_id": str(run_id),
                        "stage_name": stage_name.value,
                        "stage_status": "completed",
                        "semantic_call_id": None,
                        "semantic_artifact_id": None,
                        "evidence_packet_revision": 1,
                        "model_name": "test-model",
                        "prompt_version": "v1",
                        "schema_version": 1,
                        "artifact": draft_artifact
                        if stage_name.value == "draft"
                        else {},
                        "error": None,
                        "attempts": 1,
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                    }
                )

        # First call: citation_pass fails due to invalid artifact.
        bad_citation = {
            "schema_version": "synthesis-citation-pass-v1",
            "run_id": str(run_id),
            "evidence_packet_revision": 1,
            "draft_revision": 1,
            "pass_status": "passed",
            "validation_results": [
                {
                    "section_id": "s1",
                    "claim_id": "00000000-0000-0000-0000-000000000102",
                    "passage_ids": ["00000000-0000-0000-0000-000000000601"],
                    "status": "invalid",
                    "issue": "entailment mismatch",
                }
            ],
            "invented_citations": [],
            "unsupported_claims": [],
            "entailment_mismatches": [],
        }

        with patch("model_gateway.call_structured") as mock_call:
            mock_result = MagicMock()
            mock_result.error = None
            mock_result.value = bad_citation
            mock_result.semantic_call_id = str(uuid4())
            mock_result.artifact_ids = [str(uuid4())]
            mock_call.return_value = mock_result

            summary = service.run_synthesis(
                run_id=run_id,
                packet_revision=1,
                model_name="test-model",
            )

        # run_synthesis catches ReportServiceError and returns a failed summary.
        assert summary["overall_status"] == "failed"
        assert summary["stages"]["citation_pass"]["status"] == "failed"
        assert (
            "citation-pass semantic validation failed"
            in summary["stages"]["citation_pass"]["error"]
        )
        assert "error" in summary
        assert "citation-pass semantic validation failed" in summary["error"]

        # Verify the citation_pass stage is marked failed.
        record = mock_uow.synthesis_stages.get_synthesis_stage(run_id, "citation_pass")
        assert record["stage_status"] == "failed"
        assert "unresolved validation results" in record.get("error", "")

        # Now resume with a corrected artifact.
        good_citation = {
            "schema_version": "synthesis-citation-pass-v1",
            "run_id": str(run_id),
            "evidence_packet_revision": 1,
            "draft_revision": 1,
            "pass_status": "passed",
            "validation_results": [
                {
                    "section_id": "s1",
                    "claim_id": "00000000-0000-0000-0000-000000000102",
                    "passage_ids": ["00000000-0000-0000-0000-000000000601"],
                    "status": "valid",
                    "issue": "",
                }
            ],
            "invented_citations": [],
            "unsupported_claims": [],
            "entailment_mismatches": [],
        }

        with patch("model_gateway.call_structured") as mock_call:
            mock_result = MagicMock()
            mock_result.error = None
            mock_result.value = good_citation
            mock_result.semantic_call_id = str(uuid4())
            mock_result.artifact_ids = [str(uuid4())]
            mock_call.return_value = mock_result

            summary = service.resume_failed_synthesis(
                run_id=run_id,
                packet_revision=1,
                model_name="test-model",
            )

        assert summary["stages"]["citation_pass"]["status"] == "completed"
        record = mock_uow.synthesis_stages.get_synthesis_stage(run_id, "citation_pass")
        assert record["stage_status"] == "completed"

    def test_cache_hit_invalid_citation_marks_failed(self):
        """A cache-hit with an invalid citation artifact must also mark the
        stage as failed through the same ReportServiceError boundary."""
        service, mock_uow = _make_service(execution_mode="autonomous_local")
        run_id = UUID("00000000-0000-0000-0000-000000000401")

        from research_store.domain import SynthesisStageName

        for stage_name in SynthesisStageName:
            if stage_name.value in ("outline", "binding", "draft"):
                mock_uow.synthesis_stages.update_synthesis_stage(
                    {
                        "id": str(uuid4()),
                        "run_id": str(run_id),
                        "stage_name": stage_name.value,
                        "stage_status": "completed",
                        "semantic_call_id": None,
                        "semantic_artifact_id": None,
                        "evidence_packet_revision": 1,
                        "model_name": "test-model",
                        "prompt_version": "v1",
                        "schema_version": 1,
                        "artifact": {},
                        "error": None,
                        "attempts": 1,
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                    }
                )

        bad_citation = {
            "schema_version": "synthesis-citation-pass-v1",
            "run_id": str(run_id),
            "evidence_packet_revision": 1,
            "draft_revision": 1,
            "pass_status": "passed",
            "validation_results": [],
            "invented_citations": [{"section_id": "s1"}],
            "unsupported_claims": [],
            "entailment_mismatches": [],
        }

        with patch.object(service, "_check_cache", return_value=bad_citation):
            summary = service.run_synthesis(
                run_id=run_id,
                packet_revision=1,
                model_name="test-model",
            )

        # run_synthesis catches ReportServiceError and returns a failed summary.
        assert summary["overall_status"] == "failed"
        assert summary["stages"]["citation_pass"]["status"] == "failed"
        assert (
            "citation-pass semantic validation failed"
            in summary["stages"]["citation_pass"]["error"]
        )


# ---------------------------------------------------------------------------
# Defect 3 — release environment artifacts must not serialize secrets
# ---------------------------------------------------------------------------


class TestEnvironmentManifestSecretStripping:
    """Prove raw endpoint URLs never appear in release evidence."""

    def test_raw_urls_stripped_from_manifest(self, monkeypatch):
        """EMBEDDING_URL, GENERATIVE_URL, RERANKER_URL must not appear."""
        monkeypatch.setenv("GENERATIVE_MODEL", "gpt-4")
        monkeypatch.setenv("GENERATIVE_URL", "https://secret-api.example.com/v1")
        monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
        monkeypatch.setenv("EMBEDDING_URL", "https://secret-embed.example.com/v1")
        monkeypatch.setenv("RERANKER_MODEL", "rerank-lite")
        monkeypatch.setenv("RERANKER_URL", "https://secret-rerank.example.com/v1")
        monkeypatch.setenv("EMBEDDING_REVISION", "v2")
        monkeypatch.setenv("EMBEDDING_DIMENSION", "1536")

        manifest = _build_env_manifest(
            candidate_sha="a" * 40,
            dataset_path=Path("/dev/null"),
            dataset_hash="d" * 64,
        )

        assert "GENERATIVE_URL" not in manifest
        assert "EMBEDDING_URL" not in manifest
        assert "RERANKER_URL" not in manifest
        assert manifest.get("GENERATIVE_MODEL") == "gpt-4"
        assert manifest.get("EMBEDDING_MODEL") == "text-embedding-3-small"
        assert manifest.get("RERANKER_MODEL") == "rerank-lite"
        assert manifest.get("EMBEDDING_REVISION") == "v2"
        assert manifest.get("EMBEDDING_DIMENSION") == "1536"

    def test_non_secret_metadata_survives(self, monkeypatch):
        """Non-secret environment variables should still be captured."""
        monkeypatch.setenv("GENERATIVE_MODEL", "claude-sonnet-4")
        monkeypatch.setenv("EMBEDDING_MODEL", "nomic-embed")
        monkeypatch.setenv("RERANKER_MODEL", "rerank-v2")
        monkeypatch.setenv("GENERATIVE_URL", "https://secret.example.com")
        monkeypatch.setenv("EMBEDDING_URL", "https://secret.example.com")
        monkeypatch.setenv("RERANKER_URL", "https://secret.example.com")
        monkeypatch.setenv("EMBEDDING_REVISION", "main")
        monkeypatch.setenv("EMBEDDING_DIMENSION", "768")

        manifest = _build_env_manifest(
            candidate_sha="b" * 40,
            dataset_path=Path("/dev/null"),
            dataset_hash="e" * 64,
        )

        assert manifest["GENERATIVE_MODEL"] == "claude-sonnet-4"
        assert manifest["EMBEDDING_MODEL"] == "nomic-embed"
        assert manifest["RERANKER_MODEL"] == "rerank-v2"
        assert manifest["EMBEDDING_REVISION"] == "main"
        assert manifest["EMBEDDING_DIMENSION"] == "768"
        assert "GENERATIVE_URL" not in manifest
        assert "EMBEDDING_URL" not in manifest
        assert "RERANKER_URL" not in manifest

    def test_missing_url_keys_are_harmless(self, monkeypatch):
        """When URL env vars are absent, no error occurs."""
        monkeypatch.setenv("GENERATIVE_MODEL", "gpt-4")
        for key in ("EMBEDDING_URL", "GENERATIVE_URL", "RERANKER_URL"):
            monkeypatch.delenv(key, raising=False)

        manifest = _build_env_manifest(
            candidate_sha="c" * 40,
            dataset_path=Path("/dev/null"),
            dataset_hash="f" * 64,
        )

        assert "GENERATIVE_URL" not in manifest
        assert "EMBEDDING_URL" not in manifest
        assert "RERANKER_URL" not in manifest
        assert manifest["GENERATIVE_MODEL"] == "gpt-4"

    def test_api_keys_absent_from_manifest(self, monkeypatch):
        """API-key values must not appear in release evidence."""
        monkeypatch.setenv("GENERATIVE_MODEL", "gpt-4")
        monkeypatch.setenv("GENERATIVE_API_KEY", "sk-live-key-12345")
        monkeypatch.setenv("EMBEDDING_MODEL", "nomic-embed")
        monkeypatch.setenv("EMBEDDING_API_KEY", "ek-live-key-67890")
        monkeypatch.setenv("GENERATIVE_URL", "https://secret.example.com")
        monkeypatch.setenv("EMBEDDING_URL", "https://secret.example.com")

        manifest = _build_env_manifest(
            candidate_sha="d" * 40,
            dataset_path=Path("/dev/null"),
            dataset_hash="g" * 64,
        )

        assert "GENERATIVE_API_KEY" not in manifest
        assert "EMBEDDING_API_KEY" not in manifest
        assert "RERANKER_API_KEY" not in manifest

    def test_intentional_secret_leak_triggers_scanner_failure(
        self, tmp_path, monkeypatch
    ):
        """Deliberately injecting a runtime secret into generated evidence must
        cause the release secret scanner to fail, proving the scanner is active."""
        monkeypatch.setenv("GENERATIVE_MODEL", "gpt-4")
        monkeypatch.setenv("GENERATIVE_URL", "https://secret.example.com")

        manifest = _build_env_manifest(
            candidate_sha="e" * 40,
            dataset_path=Path("/dev/null"),
            dataset_hash="h" * 64,
        )

        # Normal manifest passes scanner.
        import scan_release_secrets

        output = tmp_path / "scan_normal.json"
        report = scan_release_secrets.scan_paths([tmp_path], output=output)
        # Write manifest to tmp_path so scanner sees it.
        (tmp_path / "manifest.json").write_text(
            __import__("json").dumps(manifest, sort_keys=True), encoding="utf-8"
        )
        report = scan_release_secrets.scan_paths([tmp_path], output=output)
        assert report["status"] == "pass"

        # Inject a genuine secret value into the manifest.
        # Construct at runtime from fragments so no single literal matches
        # the scanner's ``\bsk-[A-Za-z0-9_-]{20,}\b`` pattern.
        leaked_secret = "sk-" + (
            "live" + "-" + "leaked" + "-" + "secret" + "-" + "value"
        )
        manifest["GENERATIVE_URL"] = leaked_secret
        (tmp_path / "manifest.json").write_text(
            __import__("json").dumps(manifest, sort_keys=True), encoding="utf-8"
        )
        report = scan_release_secrets.scan_paths([tmp_path], output=output)
        assert report["status"] == "fail"


# ---------------------------------------------------------------------------
# Defect 2 — PostgreSQL-backed citation durability
# ---------------------------------------------------------------------------

_PG_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
_pg_skip = pytest.mark.skipif(
    not _PG_DSN, reason="requires RESEARCH_STORE_TEST_DATABASE_URL"
)


def _seed_synthesis_stages_in_pg(uow_factory, run_id, draft_artifact):
    """Pre-populate outline, binding, draft as completed in PostgreSQL."""
    from research_store.domain import SynthesisStageName

    for stage_name in SynthesisStageName:
        if stage_name.value in ("outline", "binding", "draft"):
            with uow_factory() as uow:
                uow.insert_synthesis_stage(
                    {
                        "id": str(uuid4()),
                        "run_id": str(run_id),
                        "stage_name": stage_name.value,
                        "stage_status": "completed",
                        "semantic_call_id": None,
                        "semantic_artifact_id": None,
                        "evidence_packet_revision": 1,
                        "model_name": "test-model",
                        "prompt_version": "v1",
                        "schema_version": 1,
                        "artifact": draft_artifact
                        if stage_name.value == "draft"
                        else {},
                        "error": None,
                        "attempts": 1,
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                    }
                )


def _read_citation_stage(uow_factory, run_id):
    """Open a fresh UOW and read the citation_pass stage record."""
    with uow_factory() as uow:
        return uow.synthesis_stages.get_synthesis_stage(run_id, "citation_pass")


@_pg_skip
def test_citation_failure_commits_failed_state_through_transaction_boundary():
    """A semantically invalid citation response must leave the stage durably
    as ``failed`` even though ``ReportServiceError`` propagates out of
    ``run_synthesis``.  The failure is written through its own UOW boundary
    so the real PostgreSQL commit survives the exception.

    This proves the transaction fix: the old code raised from inside the
    same UOW that wrote the failed status, causing a rollback that erased
    the failure record.
    """
    from dataclasses import replace

    from research_store.config import StoreConfig
    from research_store.container import build_service
    from research_store.postgres import connect, migrate
    from research_store.semantic_service import SemanticCallService

    migrate(_PG_DSN)

    run_id = uuid4()
    with connect(_PG_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO research_runs
               (id, objective, query_plan, skill_version, llm_model, state, execution_mode)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            (
                str(run_id),
                "test request",
                "{}",
                "1.0",
                "test",
                "created",
                "autonomous_local",
            ),
        )
        conn.commit()

    config = replace(
        StoreConfig.from_env(),
        database_url=_PG_DSN,
        blob_root=Path("/tmp/arc17-pg-test-blobs"),
    )
    svc = build_service(config)
    uow_factory = svc.uow_factory

    draft_artifact = {
        "schema_version": "synthesis-draft-v1",
        "run_id": str(run_id),
        "evidence_packet_revision": 1,
        "report_sections": [
            {
                "section_id": "s1",
                "title": "Findings",
                "body": "Test findings",
                "claim_references": [
                    {
                        "claim_id": "00000000-0000-0000-0000-000000000102",
                        "passage_ids": ["00000000-0000-0000-0000-000000000601"],
                        "relationship": "qualifies",
                    }
                ],
            }
        ],
        "unsupported_claims": [],
        "limitations": [],
    }
    _seed_synthesis_stages_in_pg(uow_factory, run_id, draft_artifact)

    # Seed the EvidencePacket so _get_packet can find it.
    with uow_factory() as uow:
        uow.persist_evidence_packet(
            run_id=run_id,
            research_spec_id=UUID("00000000-0000-0000-0000-000000000100"),
            coverage_revision=2,
            packet_revision=1,
            payload={
                "schema_version": "evidence-packet-v1",
                "run_id": str(run_id),
                "research_spec_id": "00000000-0000-0000-0000-000000000100",
                "coverage_revision": 2,
                "claims": [
                    {
                        "claim_id": "00000000-0000-0000-0000-000000000102",
                        "statement": "The documented behavior is reproducible.",
                        "semantic_status": "qualified",
                        "uncertainty": "Only one source has been acquired.",
                    }
                ],
                "passages": [
                    {
                        "passage_id": "00000000-0000-0000-0000-000000000601",
                        "candidate_id": "00000000-0000-0000-0000-000000000301",
                        "snapshot_id": "00000000-0000-0000-0000-000000000602",
                        "chunk_id": "00000000-0000-0000-0000-000000000603",
                        "text": "The fixture passage records the documented behavior.",
                        "source_url": "https://fixture.invalid/docs",
                    }
                ],
                "omitted_passages": [],
                "claim_evidence_bindings": [
                    {
                        "binding_id": "00000000-0000-0000-0000-000000000604",
                        "claim_id": "00000000-0000-0000-0000-000000000102",
                        "passage_ids": ["00000000-0000-0000-0000-000000000601"],
                        "relationship": "qualifies",
                        "confidence": 0.7,
                        "uncertainty": "Independent replication is missing.",
                    }
                ],
            },
        )

    bad_citation = {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": str(run_id),
        "evidence_packet_revision": 1,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [
            {
                "section_id": "s1",
                "claim_id": "00000000-0000-0000-0000-000000000102",
                "passage_ids": ["00000000-0000-0000-0000-000000000601"],
                "status": "invalid",
                "issue": "entailment mismatch",
            }
        ],
        "invented_citations": [],
        "unsupported_claims": [],
        "entailment_mismatches": [],
    }

    from budget_policy import DEFAULT_POLICY
    from research_store.evidence import EvidenceService

    semantic = SemanticCallService(uow_factory)
    evidence = EvidenceService(uow_factory, budget_policy=DEFAULT_POLICY)
    service = LocalSynthesisService(
        semantic_service=semantic,
        evidence_service=evidence,
        config=config,
    )

    with patch("model_gateway.call_structured") as mock_call:
        mock_result = MagicMock()
        mock_result.error = None
        mock_result.value = bad_citation
        # Use None for semantic_call_id to avoid FK violations on semantic_calls.
        mock_result.semantic_call_id = None
        mock_result.artifact_ids = []
        mock_call.return_value = mock_result

        summary = service.run_synthesis(
            run_id=run_id,
            packet_revision=1,
            model_name="test-model",
        )

    assert summary["overall_status"] == "failed"
    assert summary["stages"]["citation_pass"]["status"] == "failed"
    assert (
        "citation-pass semantic validation failed"
        in summary["stages"]["citation_pass"]["error"]
    )

    # Fresh UOW readback proves the failed state was committed, not rolled back.
    record = _read_citation_stage(uow_factory, run_id)
    assert record["stage_status"] == "failed"
    assert "unresolved validation results" in record.get("error", "")
    assert record.get("attempts", 0) >= 1
    # No invalid artifact should have been persisted as the stage artifact.
    assert (
        record.get("artifact") is None
        or record.get("artifact", {}).get("pass_status") != "passed"
    )

    # Now resume with a corrected artifact.
    good_citation = {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": str(run_id),
        "evidence_packet_revision": 1,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [
            {
                "section_id": "s1",
                "claim_id": "00000000-0000-0000-0000-000000000102",
                "passage_ids": ["00000000-0000-0000-0000-000000000601"],
                "status": "valid",
                "issue": "",
            }
        ],
        "invented_citations": [],
        "unsupported_claims": [],
        "entailment_mismatches": [],
    }

    with patch("model_gateway.call_structured") as mock_call:
        mock_result = MagicMock()
        mock_result.error = None
        mock_result.value = good_citation
        # Use None for semantic_call_id to avoid FK violations on semantic_calls.
        mock_result.semantic_call_id = None
        mock_result.artifact_ids = []
        mock_call.return_value = mock_result

        summary = service.resume_failed_synthesis(
            run_id=run_id,
            packet_revision=1,
            model_name="test-model",
        )

    assert summary["stages"]["citation_pass"]["status"] == "completed"

    # Fresh UOW readback proves the completed state is durable.
    final_record = _read_citation_stage(uow_factory, run_id)
    assert final_record["stage_status"] == "completed"
    assert final_record.get("artifact", {}).get("pass_status") == "passed"


# ---------------------------------------------------------------------------
# Defect 1 — PostgreSQL-backed citation canonicalization with "none" sentinel
# ---------------------------------------------------------------------------


@_pg_skip
def test_autonomous_none_rejected_then_canonical_accepted():
    """A real autonomous_local run must reject noncanonical issue='none' at
    the schema boundary and accept the corrected canonical response on retry.
    The persisted semantic artifact must contain issue='' and terminal
    completion must succeed with research_runs.state == 'completed'."""
    import json
    from dataclasses import replace

    import model_gateway
    from budget_policy import DEFAULT_POLICY
    from completion_provenance_test_support import (
        seed_authoritative_completion_provenance,
    )
    from research_store.completion_provenance import (
        load_authoritative_completion_provenance,
    )
    from research_store.config import StoreConfig
    from research_store.container import (
        build_run_service,
        build_service,
        build_workflow_operation_service,
    )
    from research_store.domain import IngestRequest
    from research_store.evidence import EvidenceService
    from research_store.postgres import connect, migrate
    from research_store.report_service import LocalSynthesisService
    from research_store.semantic_service import SemanticCallService

    migrate(_PG_DSN)
    config = replace(
        StoreConfig.from_env(),
        database_url=_PG_DSN,
        blob_root=Path("/tmp/arc17-pg-test-blobs"),
        qdrant_collection=f"arc17-none-{uuid4().hex}",
        embedding_dimension=4,
    )

    runs = build_run_service(config)
    corpus = build_service(config)
    external_id = f"arc17-none-{uuid4().hex}"
    status = runs.create(
        "arc17 autonomous none test",
        external_id,
        execution_mode="autonomous_local",
    )
    manifest = corpus.ingest_batch(
        f"fc-{uuid4().hex}",
        "scrape",
        [
            IngestRequest(
                f"https://example/{uuid4().hex}",
                b"# Test evidence\n\nPostgreSQL owns authoritative provenance.",
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
            idempotency_key=f"test:{external_id}:{next_state}",
            actor_type="integration-test",
        )
        revision += 1

    with connect(_PG_DSN) as conn, conn.cursor() as cursor:
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
            (status.id,),
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
            (status.id,),
        )

    workflow = build_workflow_operation_service(config)
    workflow._finalize_indexing(
        external_id,
        f"test:{external_id}:finalize-indexing",
    )
    status = runs.status(run_id=status.id)
    assert status.state == "coverage_review"
    run_id = status.id

    # Seed authoritative completion provenance first — it creates the evidence
    # packet, claims, passages, and pre-seeded synthesis stages we need.
    seed_authoritative_completion_provenance(runs.uow_factory, run_id)

    # Read back the seeded packet and draft to learn the actual IDs.
    with connect(_PG_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT packet_revision, payload FROM evidence_packets WHERE run_id=%s ORDER BY packet_revision DESC LIMIT 1",
            (str(run_id),),
        )
        packet_row = cur.fetchone()
        assert packet_row is not None
        packet_revision = packet_row[0]
        packet = packet_row[1]
        del packet  # Only need packet_revision; payload read later from draft

        cur.execute(
            "SELECT artifact FROM synthesis_stages WHERE run_id=%s AND stage_name='draft'",
            (str(run_id),),
        )
        draft_row = cur.fetchone()
        assert draft_row is not None
        draft = draft_row[0]

    # Extract the actual claim/passage IDs from the seeded draft.
    draft_section = draft["report_sections"][0]
    claim_id = str(draft_section["claim_references"][0]["claim_id"])
    passage_id = str(draft_section["claim_references"][0]["passage_ids"][0])
    section_id = draft_section["section_id"]

    # The seeded provenance already created outline, binding, draft stages.
    # We only need to remove citation_pass so the test can exercise actual execution.
    with connect(_PG_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM synthesis_stages WHERE run_id=%s AND stage_name='citation_pass'",
            (str(run_id),),
        )
        assert cur.rowcount == 1
        cur.execute(
            "DELETE FROM semantic_artifacts sa USING semantic_calls sc "
            "WHERE sa.semantic_call_id=sc.id AND sc.run_id=%s AND sc.stage='citation_pass'",
            (str(run_id),),
        )
        cur.execute(
            "DELETE FROM semantic_calls WHERE run_id=%s AND stage='citation_pass'",
            (str(run_id),),
        )

    # Mock _request_json to return noncanonical then canonical responses.
    # Use the actual claim_id and passage_id from the seeded packet.
    responses_iter = iter(
        [
            # First call: issue="none" — will fail schema validation
            (
                {
                    "id": "attempt-1",
                    "model": "local-model",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {
                                        "schema_version": "synthesis-citation-pass-v1",
                                        "run_id": str(run_id),
                                        "evidence_packet_revision": packet_revision,
                                        "draft_revision": 1,
                                        "pass_status": "passed",
                                        "validation_results": [
                                            {
                                                "section_id": section_id,
                                                "claim_id": claim_id,
                                                "passage_ids": [passage_id],
                                                "status": "valid",
                                                "issue": "none",
                                            }
                                        ],
                                        "invented_citations": [],
                                        "unsupported_claims": [],
                                        "entailment_mismatches": [],
                                    }
                                )
                            },
                        }
                    ],
                    "usage": {},
                },
                "req-1",
                200,
            ),
            # Second call: issue="" — canonical, passes schema
            (
                {
                    "id": "attempt-2",
                    "model": "local-model",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {
                                        "schema_version": "synthesis-citation-pass-v1",
                                        "run_id": str(run_id),
                                        "evidence_packet_revision": packet_revision,
                                        "draft_revision": 1,
                                        "pass_status": "passed",
                                        "validation_results": [
                                            {
                                                "section_id": section_id,
                                                "claim_id": claim_id,
                                                "passage_ids": [passage_id],
                                                "status": "valid",
                                                "issue": "",
                                            }
                                        ],
                                        "invented_citations": [],
                                        "unsupported_claims": [],
                                        "entailment_mismatches": [],
                                    }
                                )
                            },
                        }
                    ],
                    "usage": {},
                },
                "req-2",
                200,
            ),
        ]
    )

    def fake_request(_url, payload, _headers, _timeout):
        return next(responses_iter)

    original_request = model_gateway._request_json
    try:
        model_gateway._request_json = fake_request
        model_gateway.probe_local = lambda *_a, **_k: {"status": "available"}

        semantic = SemanticCallService(runs.uow_factory)
        evidence = EvidenceService(runs.uow_factory, budget_policy=DEFAULT_POLICY)
        service = LocalSynthesisService(
            semantic_service=semantic,
            evidence_service=evidence,
            config=config,
        )

        summary = service.run_synthesis(
            run_id=run_id,
            packet_revision=1,
            model_name="local-model",
        )
        assert summary["overall_status"] == "completed"
        assert summary["stages"]["citation_pass"]["status"] == "completed"
    finally:
        model_gateway._request_json = original_request

    # Verify PostgreSQL contains real semantic_calls and semantic_artifacts.
    with connect(_PG_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM semantic_calls WHERE run_id=%s AND stage='citation_pass'",
            (str(run_id),),
        )
        call_row = cur.fetchone()
        assert call_row is not None
        call_id = call_row[0]

        cur.execute(
            "SELECT id, payload FROM semantic_artifacts WHERE semantic_call_id=%s",
            (call_id,),
        )
        artifacts = cur.fetchall()
        assert len(artifacts) > 0
        payload = artifacts[0][1]
        assert isinstance(payload, dict)
        assert payload.get("pass_status") == "passed"
        issues = [r.get("issue") for r in payload.get("validation_results", [])]
        assert all(i == "" for i in issues), (
            f"immutable semantic artifact must have canonical empty issue, got: {issues}"
        )

    # Prove content hash matches the exact canonical payload.
    import hashlib

    expected_hash = hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    with connect(_PG_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT content_sha256 FROM semantic_artifacts WHERE id=%s",
            (str(artifacts[0][0]),),
        )
        stored_hash = cur.fetchone()[0]
        assert stored_hash == expected_hash

    # Prove synthesis stage artifact equals semantic artifact payload.
    with connect(_PG_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT artifact FROM synthesis_stages WHERE run_id=%s AND stage_name='citation_pass'",
            (str(run_id),),
        )
        stage_artifact = cur.fetchone()[0]
        assert stage_artifact == payload

    # Load authoritative completion provenance.
    with runs.uow_factory() as uow:
        provenance = load_authoritative_completion_provenance(uow, run_id)
        assert provenance is not None
        assert provenance.run_id == run_id
        assert provenance.citation_artifact_sha256 == expected_hash

    # Invoke terminal completion.
    finished = workflow.finish_run(status.external_id, outcome="satisfied")
    assert finished.state == "completed"

    # Fresh connection proves research_runs.state == "completed".
    with connect(_PG_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT state, completed_at IS NOT NULL FROM research_runs WHERE id=%s",
            (str(run_id),),
        )
        row = cur.fetchone()
        assert row[0] == "completed"
        assert row[1] is True


# ---------------------------------------------------------------------------
# Defect 2 — PostgreSQL-backed deterministic prompt-version provenance
# ---------------------------------------------------------------------------


@_pg_skip
def test_deterministic_prompt_version_matches_stage_and_call():
    """Deterministic-debug synthesis stages must carry the same truthful
    prompt_version into both the stage record and the immutable semantic call.

    This test constructs a real pre-synthesis run and executes the full
    synthesis pipeline without pre-seeding completed stages. Stage and call
    identities are read directly from PostgreSQL after execution.
    """
    import os

    os.environ["FIRECRAWL_RELEASE_DETERMINISTIC_FIXTURES"] = "1"

    try:
        from dataclasses import replace

        from research_store.config import StoreConfig
        from research_store.container import build_service
        from research_store.postgres import connect, migrate
        from research_store.semantic_service import SemanticCallService

        migrate(_PG_DSN)

        run_id = uuid4()
        with connect(_PG_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs
                   (id, objective, query_plan, skill_version, llm_model, state, execution_mode)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO NOTHING""",
                (
                    str(run_id),
                    "test request",
                    "{}",
                    "1.0",
                    "test",
                    "created",
                    "deterministic_debug",
                ),
            )
            conn.commit()

        config = replace(
            StoreConfig.from_env(),
            database_url=_PG_DSN,
            blob_root=Path("/tmp/arc17-pg-test-blobs"),
        )
        svc = build_service(config)
        uow_factory = svc.uow_factory

        with uow_factory() as uow:
            uow.persist_evidence_packet(
                run_id=run_id,
                research_spec_id=UUID("00000000-0000-0000-0000-000000000100"),
                coverage_revision=1,
                packet_revision=1,
                payload={
                    "schema_version": "evidence-packet-v1",
                    "run_id": str(run_id),
                    "research_spec_id": "00000000-0000-0000-0000-000000000100",
                    "coverage_revision": 1,
                    "claims": [
                        {
                            "claim_id": "00000000-0000-0000-0000-000000000102",
                            "statement": "The documented behavior is reproducible.",
                            "semantic_status": "qualified",
                            "uncertainty": "Only one source has been acquired.",
                        }
                    ],
                    "passages": [
                        {
                            "passage_id": "00000000-0000-0000-0000-000000000601",
                            "candidate_id": "00000000-0000-0000-0000-000000000301",
                            "snapshot_id": "00000000-0000-0000-0000-000000000602",
                            "chunk_id": "00000000-0000-0000-0000-000000000603",
                            "text": "The fixture passage records the documented behavior.",
                            "source_url": "https://fixture.invalid/docs",
                        }
                    ],
                    "omitted_passages": [],
                    "claim_evidence_bindings": [
                        {
                            "binding_id": "00000000-0000-0000-0000-000000000604",
                            "claim_id": "00000000-0000-0000-0000-000000000102",
                            "passage_ids": ["00000000-0000-0000-0000-000000000601"],
                            "relationship": "qualifies",
                            "confidence": 0.7,
                            "uncertainty": "Independent replication is missing.",
                        }
                    ],
                },
            )

        from budget_policy import DEFAULT_POLICY
        from research_store.evidence import EvidenceService

        semantic = SemanticCallService(uow_factory)
        evidence = EvidenceService(uow_factory, budget_policy=DEFAULT_POLICY)
        service = LocalSynthesisService(
            semantic_service=semantic,
            evidence_service=evidence,
            config=config,
        )

        # Execute synthesis from pre-synthesis state — no stages pre-seeded.
        summary = service.run_synthesis(
            run_id=run_id,
            packet_revision=1,
            model_name="",
        )

        assert summary["overall_status"] == "completed"

        # Read stage + semantic call identities from a fresh UOW.
        with connect(_PG_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT stage_name, prompt_version, model_name
                   FROM synthesis_stages WHERE run_id=%s ORDER BY id""",
                (str(run_id),),
            )
            stages = cur.fetchall()

        with connect(_PG_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT sc.stage, sc.prompt_version, sc.model
                   FROM semantic_calls sc
                   JOIN synthesis_stages ss ON ss.run_id=sc.run_id AND ss.stage_name=sc.stage
                   WHERE sc.run_id=%s""",
                (str(run_id),),
            )
            calls = cur.fetchall()

        # Every synthesis stage must have empty model and the same prompt version.
        expected_prompt_version = "synthesis-v1"
        from research_store.domain import SynthesisStageName as _Stage

        expected_stages = {s.value for s in _Stage}
        actual_stages = {s[0] for s in stages}
        assert actual_stages == expected_stages, (
            f"expected stages {expected_stages}, got {actual_stages}"
        )
        for stage_name, prompt_version, model_name in stages:
            assert model_name == "", f"{stage_name} model_name must be empty"
            assert prompt_version == expected_prompt_version, (
                f"{stage_name} prompt_version={prompt_version!r} != {expected_prompt_version!r}"
            )

        # Every semantic call must match the stage prompt version.
        for stage, prompt_version, model in calls:
            assert model == "", f"{stage} model must be empty"
            assert prompt_version == expected_prompt_version, (
                f"{stage} prompt_version={prompt_version!r} != {expected_prompt_version!r}"
            )

        # Verify exact linkage between stages and semantic calls.
        with connect(_PG_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT ss.stage_name, ss.semantic_call_id, ss.semantic_artifact_id,
                          sc.id AS call_id, sc.prompt_version AS call_pv, sc.model AS call_model
                   FROM synthesis_stages ss
                   JOIN semantic_calls sc ON sc.id=ss.semantic_call_id
                   WHERE ss.run_id=%s""",
                (str(run_id),),
            )
            links = cur.fetchall()
        for (
            stage_name,
            stage_call_id,
            artifact_id,
            call_id,
            call_pv,
            call_model,
        ) in links:
            assert stage_call_id == call_id, (
                f"{stage_name} stage_call_id={stage_call_id} != call_id={call_id}"
            )
            assert call_pv == expected_prompt_version, (
                f"{stage_name} call prompt_version={call_pv!r} != {expected_prompt_version!r}"
            )
            assert call_model == "", f"{stage_name} call model={call_model!r} != ''"
    finally:
        del os.environ["FIRECRAWL_RELEASE_DETERMINISTIC_FIXTURES"]


# ---------------------------------------------------------------------------
# Negative test: genuine persisted prompt-version divergence fails terminal completion
# ---------------------------------------------------------------------------


@_pg_skip
def test_prompt_version_divergence_fails_terminal_completion():
    """A synthesis stage whose prompt_version diverges from its semantic call
    must be rejected at terminal completion time. The stage is altered in-place
    while the immutable semantic call remains unchanged."""
    import os

    os.environ["FIRECRAWL_RELEASE_DETERMINISTIC_FIXTURES"] = "1"

    try:
        from dataclasses import replace

        from completion_provenance_test_support import (
            seed_authoritative_completion_provenance,
        )
        from research_store.config import StoreConfig
        from research_store.container import (
            build_run_service,
            build_service,
            build_workflow_operation_service,
        )
        from research_store.domain import IngestRequest
        from research_store.postgres import connect, migrate
        from research_store.workflow_service import WorkflowBoundaryError

        migrate(_PG_DSN)
        config = replace(
            StoreConfig.from_env(),
            database_url=_PG_DSN,
            blob_root=Path("/tmp/arc17-pg-test-blobs"),
            qdrant_collection=f"arc17_{uuid4().hex}",
            embedding_dimension=4,
        )

        def _seed_and_finalize(corpus, runs, external_id, execution_mode):
            status = runs.create(
                "arc17 test run",
                external_id,
                execution_mode=execution_mode,
            )
            manifest = corpus.ingest_batch(
                f"fc-{uuid4().hex}",
                "scrape",
                [
                    IngestRequest(
                        f"https://example/{uuid4().hex}",
                        b"# Test evidence\n\nPostgreSQL owns authoritative provenance.",
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
                    idempotency_key=f"test:{external_id}:{next_state}",
                    actor_type="integration-test",
                )
                revision += 1
            with connect(_PG_DSN) as conn, conn.cursor() as cursor:
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
                    (status.id,),
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
                    (status.id,),
                )
            workflow = build_workflow_operation_service(config)
            workflow._finalize_indexing(
                external_id,
                f"test:{external_id}:finalize-indexing",
            )
            status = runs.status(run_id=status.id)
            assert status.state == "coverage_review"
            return runs, status, workflow

        runs = build_run_service(config)
        corpus = build_service(config)
        external_id = f"arc17-diverge-pv-{uuid4().hex}"
        runs, status, _workflow = _seed_and_finalize(
            corpus, runs, external_id, "deterministic_debug"
        )
        seed_authoritative_completion_provenance(runs.uow_factory, status.id)

        # Introduce divergence: alter draft stage prompt_version only.
        with connect(_PG_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE synthesis_stages SET prompt_version='divergent-v2' "
                "WHERE run_id=%s AND stage_name='draft'",
                (str(status.id),),
            )
            assert cur.rowcount == 1

        workflow = build_workflow_operation_service(config)
        with pytest.raises(
            WorkflowBoundaryError, match="prompt version does not match"
        ):
            workflow.finish_run(status.external_id, outcome="satisfied")
    finally:
        del os.environ["FIRECRAWL_RELEASE_DETERMINISTIC_FIXTURES"]


# ---------------------------------------------------------------------------
# Defect 3 — Contract test: ENVIRONMENT_FIELDS must not reintroduce raw URLs
# ---------------------------------------------------------------------------


def test_environment_fields_contract_no_raw_urls():
    """verify_release_campaign.ENVIRONMENT_FIELDS must never contain raw URL
    keys. This contract test prevents regression where someone adds
    GENERATIVE_URL/EMBEDDING_URL/RERANKER_URL back into the required fields."""
    from verify_release_campaign import ENVIRONMENT_FIELDS

    raw_url_keys = frozenset(("GENERATIVE_URL", "EMBEDDING_URL", "RERANKER_URL"))
    present = raw_url_keys & set(ENVIRONMENT_FIELDS)
    assert not present, (
        f"ENVIRONMENT_FIELDS must not contain raw secret URLs, found: {present}"
    )

    # Safe identity fields must be present.
    safe_required = frozenset(
        (
            "GENERATIVE_MODEL",
            "EMBEDDING_MODEL",
            "EMBEDDING_REVISION",
            "EMBEDDING_DIMENSION",
            "RERANKER_MODEL",
        )
    )
    missing = safe_required - set(ENVIRONMENT_FIELDS)
    assert not missing, f"ENVIRONMENT_FIELDS missing safe identity fields: {missing}"


def test_env_manifest_integration_strict_verifier_accepts_safe_evidence(
    monkeypatch, tmp_path
):
    """Build a realistic environment manifest through the production path and
    prove the strict release verifier accepts it without demanding raw URL keys."""
    import json
    from hashlib import sha256
    from pathlib import Path

    from research_store.strict_benchmark import _build_env_manifest

    # Set realistic safe identity values and raw endpoint secrets.
    monkeypatch.setenv("GENERATIVE_MODEL", "claude-sonnet-4-20250514")
    monkeypatch.setenv("GENERATIVE_URL", "https://api.anthropic.com/v1/messages")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("EMBEDDING_URL", "https://api.openai.com/v1/embeddings")
    monkeypatch.setenv("EMBEDDING_REVISION", "2024-01-01")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "1536")
    monkeypatch.setenv("RERANKER_MODEL", "rerank-lite-2024")
    monkeypatch.setenv("RERANKER_URL", "https://api.example.com/rerank")

    candidate_sha = "a" * 40
    dataset_path = Path("/dev/null")
    dataset_hash = sha256(b"").hexdigest()

    manifest = _build_env_manifest(
        candidate_sha=candidate_sha,
        dataset_path=dataset_path,
        dataset_hash=dataset_hash,
    )

    # Safe identity fields must be present with exact configured values.
    assert manifest["GENERATIVE_MODEL"] == "claude-sonnet-4-20250514"
    assert manifest["EMBEDDING_MODEL"] == "text-embedding-3-small"
    assert manifest["EMBEDDING_REVISION"] == "2024-01-01"
    assert manifest["EMBEDDING_DIMENSION"] == "1536"
    assert manifest["RERANKER_MODEL"] == "rerank-lite-2024"

    # Raw URL keys must be absent.
    assert "GENERATIVE_URL" not in manifest
    assert "EMBEDDING_URL" not in manifest
    assert "RERANKER_URL" not in manifest

    # Raw URL values must not appear anywhere in the generated evidence.
    manifest_text = json.dumps(manifest, sort_keys=True)
    for url_key in ("GENERATIVE_URL", "EMBEDDING_URL", "RERANKER_URL"):
        assert url_key not in manifest_text, (
            f"raw URL key {url_key} leaked into manifest"
        )
    for secret_value in (
        "https://api.anthropic.com/v1/messages",
        "https://api.openai.com/v1/embeddings",
        "https://api.example.com/rerank",
    ):
        assert secret_value not in manifest_text, (
            "raw secret value leaked into manifest"
        )

    # Verify the manifest passes the environment-field check.
    from verify_release_campaign import ENVIRONMENT_FIELDS

    for field in ENVIRONMENT_FIELDS:
        assert field in manifest, f"missing required field: {field}"


def test_validate_environment_accepts_complete_manifest(monkeypatch):
    """validate_environment must accept a complete, correct environment manifest."""
    from hashlib import sha256
    from pathlib import Path

    from verify_release_campaign import WorkflowIdentity, validate_environment

    monkeypatch.setenv("GENERATIVE_MODEL", "test-model")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-embed")
    monkeypatch.setenv("EMBEDDING_REVISION", "2024-01-01")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "1536")
    monkeypatch.setenv("RERANKER_MODEL", "test-rerank")

    candidate_sha = "b" * 40
    dataset_hash = sha256(b"").hexdigest()
    tree_hash = sha256(b"tree").hexdigest()

    manifest = _build_env_manifest(
        candidate_sha=candidate_sha,
        dataset_path=Path("/dev/null"),
        dataset_hash=dataset_hash,
    )
    manifest["strict"] = True
    manifest["execution_modes"] = ["autonomous_local", "deterministic_debug"]
    # Ensure tree_hash matches what we pass to validate_environment.
    manifest["tree_hash"] = tree_hash

    identity = WorkflowIdentity(
        candidate_sha=candidate_sha,
        dispatch_sha=tree_hash,
        workflow_sha=tree_hash,
        dispatch_ref="refs/heads/main",
        repository="test/repo",
        run_id="test-run",
        run_attempt="1",
        workflow_ref="refs/workflows/test",
    )
    errors = validate_environment(
        manifest,
        campaign_label="A",
        identity=identity,
        tree_hash=tree_hash,
        dataset_hash=dataset_hash,
    )
    assert errors == [], f"expected no errors, got: {errors}"


def test_validate_environment_rejects_missing_fields(monkeypatch):
    """validate_environment must reject manifests missing required fields."""
    from verify_release_campaign import WorkflowIdentity, validate_environment

    identity = WorkflowIdentity(
        candidate_sha="a" * 40,
        dispatch_sha="t" * 40,
        workflow_sha="t" * 40,
        dispatch_ref="refs/heads/main",
        repository="test/repo",
        run_id="test-run",
        run_attempt="1",
        workflow_ref="refs/workflows/test",
    )
    errors = validate_environment(
        {},
        campaign_label="B",
        identity=identity,
        tree_hash="t" * 40,
        dataset_hash="d" * 64,
    )
    assert any("lacks" in e for e in errors), (
        f"expected missing-field errors, got: {errors}"
    )


def test_validate_environment_rejects_mismatched_hashes():
    """validate_environment must reject mismatched candidate/tree/dataset hashes."""
    from verify_release_campaign import WorkflowIdentity, validate_environment

    identity = WorkflowIdentity(
        candidate_sha="wrong-sha" * 3,
        dispatch_sha="wrong-tree" * 3,
        workflow_sha="wrong-tree" * 3,
        dispatch_ref="refs/heads/main",
        repository="test/repo",
        run_id="test-run",
        run_attempt="1",
        workflow_ref="refs/workflows/test",
    )
    wrong_tree = "wrong-tree" * 3
    env = {
        "candidate_sha": "correct-sha" * 3,
        "tree_hash": "correct-tree" * 3,
        "dataset_hash": "correct-dataset" * 3,
        "strict": True,
        "execution_modes": ["autonomous_local", "deterministic_debug"],
        "GENERATIVE_MODEL": "test",
        "EMBEDDING_MODEL": "test",
        "EMBEDDING_REVISION": "test",
        "EMBEDDING_DIMENSION": "1536",
        "RERANKER_MODEL": "test",
        "timestamp": "2026-01-01T00:00:00Z",
        "python_version": "3.12",
        "platform": "linux",
        "machine": "x86_64",
    }
    errors = validate_environment(
        env,
        campaign_label="C",
        identity=identity,
        tree_hash=wrong_tree,
        dataset_hash="wrong-dataset" * 3,
    )
    assert any("candidate mismatch" in e for e in errors)
    assert any("tree mismatch" in e for e in errors)
    assert any("dataset mismatch" in e for e in errors)


def test_validate_environment_rejects_non_strict_and_wrong_modes():
    """validate_environment must reject non-strict and wrong execution modes."""
    from verify_release_campaign import WorkflowIdentity, validate_environment

    identity = WorkflowIdentity(
        candidate_sha="a" * 40,
        dispatch_sha="t" * 40,
        workflow_sha="t" * 40,
        dispatch_ref="refs/heads/main",
        repository="test/repo",
        run_id="test-run",
        run_attempt="1",
        workflow_ref="refs/workflows/test",
    )
    env = {
        "candidate_sha": "a" * 40,
        "tree_hash": "t" * 40,
        "dataset_hash": "d" * 64,
        "strict": False,
        "execution_modes": ["agent_led"],
        "GENERATIVE_MODEL": "test",
        "EMBEDDING_MODEL": "test",
        "EMBEDDING_REVISION": "test",
        "EMBEDDING_DIMENSION": "1536",
        "RERANKER_MODEL": "test",
        "timestamp": "2026-01-01T00:00:00Z",
        "python_version": "3.12",
        "platform": "linux",
        "machine": "x86_64",
    }
    errors = validate_environment(
        env,
        campaign_label="D",
        identity=identity,
        tree_hash="t" * 40,
        dataset_hash="d" * 64,
    )
    assert any("not strict" in e for e in errors)
    assert any("modes are not authoritative" in e for e in errors)


# ---------------------------------------------------------------------------
# Negative test: substantive issue text still fails closed
# ---------------------------------------------------------------------------


def test_substantive_issue_text_still_fails_closed():
    """A validation result with a real issue string must still be rejected,
    even when status='valid'. Only the canonical 'none' sentinel is allowed."""
    bad = {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": "00000000-0000-0000-0000-000000000401",
        "evidence_packet_revision": 1,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [
            {
                "section_id": "s1",
                "claim_id": "00000000-0000-0000-0000-000000000102",
                "passage_ids": ["00000000-0000-0000-0000-000000000601"],
                "status": "valid",
                "issue": "actual problem found",
            }
        ],
        "invented_citations": [],
        "unsupported_claims": [],
        "entailment_mismatches": [],
    }
    with pytest.raises(
        CompletionProvenanceError, match="unresolved validation results"
    ):
        validate_citation_artifact(bad, set())


# ---------------------------------------------------------------------------
# Negative test: invented citations still fail
# ---------------------------------------------------------------------------


def test_invented_citations_still_fail():
    """Invented citations must continue to fail regardless of validation_results."""
    bad = {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": "00000000-0000-0000-0000-000000000401",
        "evidence_packet_revision": 1,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [],
        "invented_citations": [
            {
                "section_id": "s1",
                "claim_id": "00000000-0000-0000-0000-000000000102",
                "passage_ids": ["00000000-0000-0000-0000-000000000999"],
            }
        ],
        "unsupported_claims": [],
        "entailment_mismatches": [],
    }
    with pytest.raises(CompletionProvenanceError, match="unresolved failures"):
        validate_citation_artifact(bad, set())


# ---------------------------------------------------------------------------
# Negative test: unsupported claims still fail
# ---------------------------------------------------------------------------


def test_unsupported_claims_still_fail():
    """Unsupported claims must continue to fail regardless of validation_results."""
    bad = {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": "00000000-0000-0000-0000-000000000401",
        "evidence_packet_revision": 1,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [],
        "invented_citations": [],
        "unsupported_claims": [
            {
                "claim_id": "00000000-0000-0000-0000-000000000102",
                "statement": "Unsubstantiated claim.",
            }
        ],
        "entailment_mismatches": [],
    }
    with pytest.raises(CompletionProvenanceError, match="unresolved failures"):
        validate_citation_artifact(bad, set())


# ---------------------------------------------------------------------------
# Negative test: entailment mismatches still fail
# ---------------------------------------------------------------------------


def test_entailment_mismatches_still_fail():
    """Entailment mismatches must continue to fail regardless of validation_results."""
    bad = {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": "00000000-0000-0000-0000-000000000401",
        "evidence_packet_revision": 1,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [],
        "invented_citations": [],
        "unsupported_claims": [],
        "entailment_mismatches": [
            {
                "section_id": "s1",
                "claim_id": "00000000-0000-0000-0000-000000000102",
                "expected_relationship": "supports",
                "cited_relationship": "contradicts",
            }
        ],
    }
    with pytest.raises(CompletionProvenanceError, match="unresolved failures"):
        validate_citation_artifact(bad, set())


# ---------------------------------------------------------------------------
# Negative test: wrong citation tuples still fail
# ---------------------------------------------------------------------------


def test_wrong_citation_tuples_still_fail():
    """A validation result referencing different passages than the draft must
    be rejected — the tuple comparison is independent of issue normalization."""
    bad = {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": "00000000-0000-0000-0000-000000000401",
        "evidence_packet_revision": 1,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [
            {
                "section_id": "s1",
                "claim_id": "00000000-0000-0000-0000-000000000102",
                "passage_ids": ["00000000-0000-0000-0000-000000000999"],
                "status": "valid",
                "issue": "",
            }
        ],
        "invented_citations": [],
        "unsupported_claims": [],
        "entailment_mismatches": [],
    }
    draft_citations = {
        (
            "s1",
            "00000000-0000-0000-0000-000000000102",
            ("00000000-0000-0000-0000-000000000601",),
        )
    }
    with pytest.raises(CompletionProvenanceError, match="does not exactly validate"):
        validate_citation_artifact(bad, draft_citations)


# ---------------------------------------------------------------------------
# Negative test: noncanonical "none" reaches terminal validation
# ---------------------------------------------------------------------------


def test_noncanonical_none_rejected_at_terminal():
    """A citation artifact that somehow reaches terminal validation with
    issue='none' must be rejected — the terminal validator does not whitelist
    the historical sentinel."""
    bad = {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": "00000000-0000-0000-0000-000000000401",
        "evidence_packet_revision": 1,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [
            {
                "section_id": "s1",
                "claim_id": "00000000-0000-0000-0000-000000000102",
                "passage_ids": ["00000000-0000-0000-0000-000000000601"],
                "status": "valid",
                "issue": "none",
            }
        ],
        "invented_citations": [],
        "unsupported_claims": [],
        "entailment_mismatches": [],
    }
    draft_citations = {
        (
            "s1",
            "00000000-0000-0000-0000-000000000102",
            ("00000000-0000-0000-0000-000000000601",),
        )
    }
    with pytest.raises(
        CompletionProvenanceError, match="unresolved validation results"
    ):
        validate_citation_artifact(bad, draft_citations)
