"""Regression tests for ARC-17 corrective defects.

Covers:
* Defect 1 — deterministic semantic model identity (deterministic_debug must
  not falsely claim production LLM authority).
* Defect 2 — citation-stage acceptance weaker than terminal provenance.
* Defect 3 — release environment artifacts must not serialize GitHub secret
  endpoint URLs.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock
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
    mock_config.embedding_model = "test-model"
    mock_config.generative_model = "test-model"

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

    def test_divergent_stage_model_fails_closed(self):
        """In deterministic_debug mode, model_name is forced to empty string."""
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
            model_name="divergent-model",
        )
        assert summary["overall_status"] == "completed"


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

    def test_non_secret_metadata_survives(self, monkeypatch):
        """Non-secret environment variables should still be captured."""
        monkeypatch.setenv("GENERATIVE_MODEL", "claude-sonnet-4")
        monkeypatch.setenv("EMBEDDING_MODEL", "nomic-embed")
        monkeypatch.setenv("RERANKER_MODEL", "rerank-v2")
        monkeypatch.setenv("GENERATIVE_URL", "https://secret.example.com")
        monkeypatch.setenv("EMBEDDING_URL", "https://secret.example.com")
        monkeypatch.setenv("RERANKER_URL", "https://secret.example.com")

        manifest = _build_env_manifest(
            candidate_sha="b" * 40,
            dataset_path=Path("/dev/null"),
            dataset_hash="e" * 64,
        )

        assert manifest["GENERATIVE_MODEL"] == "claude-sonnet-4"
        assert manifest["EMBEDDING_MODEL"] == "nomic-embed"
        assert manifest["RERANKER_MODEL"] == "rerank-v2"
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
