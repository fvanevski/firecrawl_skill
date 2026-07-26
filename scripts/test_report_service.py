"""Tests for the bounded autonomous-local synthesis service (issue #63)."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from research_store.domain import (
    SynthesisStageName,
    SynthesisStageRecord,
)
from research_store.report_service import (
    CommercialFallbackError,
    LocalSynthesisService,
    ReportServiceError,
)

# ---------------------------------------------------------------------------
# Fixtures
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
    """Build a mock UOW with synthesis_stages methods.

    Returns:
        (mock_uow_factory, mock_uow, records_dict)
    """
    mock_uow = MagicMock()
    mock_uow.runs.get_run_status.return_value = {
        "lifecycle_revision": 1,
        "execution_mode": "autonomous_local",
        "state": "synthesizing",
    }

    # Store records in a dict keyed by (run_id, stage_name).
    _records: dict[tuple[str, str], dict] = {}

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

    mock_ctx = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_uow)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    mock_uow_factory = MagicMock(return_value=mock_ctx)

    return mock_uow_factory, mock_uow, _records


def _make_service(
    mock_packet: dict | None = None,
    run_state: str = "synthesizing",
) -> tuple[LocalSynthesisService, MagicMock, MagicMock, MagicMock]:
    """Build a LocalSynthesisService with mocked dependencies."""
    packet = mock_packet or deepcopy(_VALID_PACKET)

    mock_evidence = MagicMock()
    mock_evidence.export_packet.return_value = packet

    # Call _make_mock_uow ONCE so the factory and mock_uow share the same
    # _records dict used by the closure-based repository methods.
    mock_uow_factory, mock_uow, _records = _make_mock_uow()

    mock_semantic = MagicMock()
    mock_semantic.uow_factory = mock_uow_factory

    mock_config = MagicMock()
    mock_config.embedding_model = "test-model"
    mock_config.database_url = "postgresql://localhost/test"
    mock_config.qdrant_url = "http://localhost:6333"
    mock_config.qdrant_api_key = ""
    mock_config.qdrant_collection = "test"
    mock_config.qdrant_alias = "test"
    mock_config.valkey_url = "redis://localhost:6379/0"
    mock_config.blob_root = MagicMock()
    mock_config.scratch_root = MagicMock()
    mock_config.embedding_model = "test-model"
    mock_config.embedding_url = ""
    mock_config.embedding_api_key = ""
    mock_config.embedding_revision = "main"
    mock_config.embedding_dimension = 1024
    mock_config.reranker_url = ""
    mock_config.reranker_model = "rerank"
    mock_config.reranker_api_key = ""
    mock_config.reranker_candidate_limit = 40
    mock_config.chunker_name = "hierarchical"
    mock_config.chunker_version = "structural-v1"
    mock_config.chunker_max_tokens = 1000
    mock_config.tokenizer_name = "cl100k_base"
    mock_config.parser_version = "markdown-v1"
    mock_config.normalization_version = "cleanup-v1"
    mock_config.parser_registry_version = "canonical-v1"
    mock_config.max_index_attempts = 5
    mock_config.job_lease_seconds = 300
    mock_config.worker_poll_seconds = 5

    service = LocalSynthesisService(
        semantic_service=mock_semantic,
        evidence_service=mock_evidence,
        config=mock_config,
    )
    return service, mock_evidence, mock_semantic, mock_uow


# ---------------------------------------------------------------------------
# Schema loading tests
# ---------------------------------------------------------------------------


def test_load_schemas_finds_files():
    """Service should load all synthesis schemas from disk."""
    service, _, _, _ = _make_service()
    assert "synthesis-outline-v1.json" in service._schemas
    assert "synthesis-draft-v1.json" in service._schemas
    assert "synthesis-citation-pass-v1.json" in service._schemas
    assert "claim-binding-v1.json" in service._schemas


def test_get_schema_returns_copy():
    """_get_schema should return a copy so mutations don't affect stored schemas."""
    service, _, _, _ = _make_service()
    schema1 = service._get_schema("outline")
    schema2 = service._get_schema("outline")
    schema1["properties"]["test"] = "injected"
    assert "test" not in schema2["properties"]


# ---------------------------------------------------------------------------
# EvidencePacket validation tests
# ---------------------------------------------------------------------------


def test_validate_packet_none_raises():
    service, _, _, _ = _make_service()
    with pytest.raises(ReportServiceError, match="EvidencePacket is None"):
        service._validate_packet(None)


def test_validate_packet_wrong_version_raises():
    service, _, _, _ = _make_service()
    bad_packet = dict(_VALID_PACKET)
    bad_packet["schema_version"] = "evidence-packet-v0"
    with pytest.raises(ReportServiceError, match="unsupported EvidencePacket version"):
        service._validate_packet(bad_packet)


def test_validate_packet_no_claims_raises():
    service, _, _, _ = _make_service()
    bad_packet = dict(_VALID_PACKET)
    bad_packet["claims"] = []
    with pytest.raises(ReportServiceError, match="no claims"):
        service._validate_packet(bad_packet)


def test_validate_packet_no_passages_raises():
    service, _, _, _ = _make_service()
    bad_packet = dict(_VALID_PACKET)
    bad_packet["passages"] = []
    with pytest.raises(ReportServiceError, match="no passages"):
        service._validate_packet(bad_packet)


def test_validate_packet_valid():
    """Valid packet should not raise."""
    service, _, _, _ = _make_service()
    service._validate_packet(_VALID_PACKET)  # no exception


# ---------------------------------------------------------------------------
# Commercial fallback tests
# ---------------------------------------------------------------------------


def test_enforce_local_only_allows_local():
    service, _, _, _ = _make_service()
    service._enforce_local_only("local")  # no exception


def test_enforce_local_only_rejects_openai():
    service, _, _, _ = _make_service()
    with pytest.raises(CommercialFallbackError, match="commercial provider 'openai'"):
        service._enforce_local_only("openai")


def test_enforce_local_only_rejects_gemini():
    service, _, _, _ = _make_service()
    with pytest.raises(CommercialFallbackError, match="commercial provider 'gemini'"):
        service._enforce_local_only("gemini")


# ---------------------------------------------------------------------------
# Domain model tests
# ---------------------------------------------------------------------------


def test_synthesis_stage_name_order():
    assert SynthesisStageName.OUTLINE.order == 1
    assert SynthesisStageName.BINDING.order == 2
    assert SynthesisStageName.DRAFT.order == 3
    assert SynthesisStageName.CITATION_PASS.order == 4


def test_synthesis_stage_name_schema_file():
    assert SynthesisStageName.OUTLINE.schema_file == "synthesis-outline-v1.json"
    assert SynthesisStageName.BINDING.schema_file == "claim-binding-v1.json"
    assert SynthesisStageName.DRAFT.schema_file == "synthesis-draft-v1.json"
    assert (
        SynthesisStageName.CITATION_PASS.schema_file
        == "synthesis-citation-pass-v1.json"
    )


def test_synthesis_stage_record_validation():
    """Invalid stage_name should raise ValueError."""
    with pytest.raises(ValueError, match="invalid stage_name"):
        SynthesisStageRecord(
            id=uuid4(),
            run_id=uuid4(),
            stage_name="invalid_stage",
            stage_status="completed",
            semantic_call_id=None,
            semantic_artifact_id=None,
            evidence_packet_revision=1,
            model_name="test",
            prompt_version="v1",
            schema_version=1,
            artifact=None,
            error=None,
            attempts=1,
            created_at=MagicMock(),
            updated_at=MagicMock(),
        )


def test_synthesis_stage_record_invalid_status():
    with pytest.raises(ValueError, match="invalid stage_status"):
        SynthesisStageRecord(
            id=uuid4(),
            run_id=uuid4(),
            stage_name="outline",
            stage_status="invalid_status",
            semantic_call_id=None,
            semantic_artifact_id=None,
            evidence_packet_revision=1,
            model_name="test",
            prompt_version="v1",
            schema_version=1,
            artifact=None,
            error=None,
            attempts=1,
            created_at=MagicMock(),
            updated_at=MagicMock(),
        )


def test_synthesis_stage_record_invalid_revision():
    with pytest.raises(ValueError, match="evidence_packet_revision must be >= 1"):
        SynthesisStageRecord(
            id=uuid4(),
            run_id=uuid4(),
            stage_name="outline",
            stage_status="completed",
            semantic_call_id=None,
            semantic_artifact_id=None,
            evidence_packet_revision=0,
            model_name="test",
            prompt_version="v1",
            schema_version=1,
            artifact=None,
            error=None,
            attempts=1,
            created_at=MagicMock(),
            updated_at=MagicMock(),
        )


# ---------------------------------------------------------------------------
# Pipeline tests (mocked LLM calls)
# ---------------------------------------------------------------------------


def test_run_synthesis_no_model_raises():
    """If no model is configured, run_synthesis should raise."""
    service, _, _, _ = _make_service()
    service.config.embedding_model = ""
    with pytest.raises(ReportServiceError, match="no model configured"):
        service.run_synthesis(
            run_id=UUID(_VALID_PACKET["run_id"]),
            packet_revision=1,
        )


def test_run_synthesis_skips_completed_stages():
    """Completed stages should be skipped on resume."""
    service, _mock_evidence, _mock_semantic, mock_uow = _make_service()
    run_id = UUID(_VALID_PACKET["run_id"])

    # Pre-populate all stages as completed.
    for stage_name in SynthesisStageName:
        record = {
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
            "artifact": {"status": "completed"},
            "error": None,
            "attempts": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        mock_uow.synthesis_stages.update_synthesis_stage(record)

    # Mock call_structured to return a valid outline.
    mock_result = MagicMock()
    mock_result.error = None
    mock_result.value = {
        "schema_version": "synthesis-outline-v1",
        "run_id": str(run_id),
        "evidence_packet_revision": 2,
        "outline_sections": [],
        "unsupported_claims": [],
    }
    mock_result.semantic_call_id = str(uuid4())
    mock_result.artifact_ids = [str(uuid4())]

    with patch("model_gateway.call_structured", return_value=mock_result):
        summary = service.run_synthesis(
            run_id=run_id,
            packet_revision=2,
            model_name="test-model",
        )

    # All stages should be skipped.
    stages = summary["stages"]
    for stage_key in ("outline", "binding", "draft", "citation_pass"):
        assert stages[stage_key]["status"] == "skipped"
    assert summary["overall_status"] == "completed"


def test_run_synthesis_outline_failure_marks_remaining():
    """If outline fails, remaining stages should be marked failed."""
    service, _mock_evidence, _mock_semantic, _mock_uow = _make_service()

    mock_result = MagicMock()
    mock_result.error = "model timeout"
    mock_result.value = None
    mock_result.semantic_call_id = str(uuid4())
    mock_result.artifact_ids = []

    with patch("model_gateway.call_structured", return_value=mock_result):
        summary = service.run_synthesis(
            run_id=UUID(_VALID_PACKET["run_id"]),
            packet_revision=1,
            model_name="test-model",
        )

    assert summary["overall_status"] == "failed"
    assert summary["stages"]["outline"]["status"] == "failed"
    # Remaining stages should be marked failed too.
    for stage_key in ("binding", "draft", "citation_pass"):
        assert summary["stages"][stage_key]["status"] == "failed"


def test_run_synthesis_resume_retries_failed():
    """Failed stages should be retried on resume."""
    service, _mock_evidence, _mock_semantic, mock_uow = _make_service()
    run_id = UUID(_VALID_PACKET["run_id"])

    # Pre-populate outline as failed, binding as completed (skip binding).
    record = {
        "id": str(uuid4()),
        "run_id": str(run_id),
        "stage_name": "outline",
        "stage_status": "failed",
        "semantic_call_id": None,
        "semantic_artifact_id": None,
        "evidence_packet_revision": 1,
        "model_name": "test-model",
        "prompt_version": "v1",
        "schema_version": 1,
        "artifact": None,
        "error": "model timeout",
        "attempts": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    mock_uow.synthesis_stages.update_synthesis_stage(record)

    # Pre-populate binding as completed so it's skipped.
    binding_record = {
        "id": str(uuid4()),
        "run_id": str(run_id),
        "stage_name": "binding",
        "stage_status": "completed",
        "semantic_call_id": None,
        "semantic_artifact_id": None,
        "evidence_packet_revision": 1,
        "model_name": "test-model",
        "prompt_version": "v1",
        "schema_version": 1,
        "artifact": {"new_packet_revision": 2},
        "error": None,
        "attempts": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    mock_uow.synthesis_stages.update_synthesis_stage(binding_record)

    # Now make the LLM succeed on retry.
    mock_result = MagicMock()
    mock_result.error = None
    mock_result.value = {
        "schema_version": "synthesis-outline-v1",
        "run_id": str(run_id),
        "evidence_packet_revision": 2,
        "outline_sections": [],
        "unsupported_claims": [],
    }
    mock_result.semantic_call_id = str(uuid4())
    mock_result.artifact_ids = [str(uuid4())]

    with patch("model_gateway.call_structured", return_value=mock_result):
        summary = service.resume_failed_synthesis(
            run_id=run_id,
            packet_revision=2,
            model_name="test-model",
        )

    # Outline should now be completed (retried).
    assert summary["stages"]["outline"]["status"] == "completed"
    assert summary["stages"]["binding"]["status"] == "skipped"
    assert summary["overall_status"] == "completed"


def test_get_stage_status_returns_records():
    """get_stage_status should return stage records."""
    service, _, _, _mock_uow = _make_service()
    run_id = UUID(_VALID_PACKET["run_id"])

    # Pre-populate one stage using the same mock UOW the service uses.
    record = {
        "id": str(uuid4()),
        "run_id": str(run_id),
        "stage_name": "outline",
        "stage_status": "completed",
        "semantic_call_id": None,
        "semantic_artifact_id": None,
        "evidence_packet_revision": 1,
        "model_name": "test-model",
        "prompt_version": "v1",
        "schema_version": 1,
        "artifact": {"status": "completed"},
        "error": None,
        "attempts": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    # Use the service's UOW factory to ensure we're updating the right records dict.
    with service.semantic.uow_factory() as uow:
        uow.synthesis_stages.update_synthesis_stage(record)

    statuses = service.get_stage_status(
        uow_factory=service.semantic.uow_factory,
        run_id=run_id,
    )
    assert len(statuses) == 1
    assert statuses[0]["stage_name"] == "outline"
    assert statuses[0]["stage_status"] == "completed"


def test_get_stage_status_filtered():
    """get_stage_status with stage_name filter should return only that stage."""
    service, _, _, _ = _make_service()
    run_id = UUID(_VALID_PACKET["run_id"])

    statuses = service.get_stage_status(
        uow_factory=service.semantic.uow_factory,
        run_id=run_id,
        stage_name="outline",
    )
    assert len(statuses) == 0  # No outline stage pre-populated


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


def test_synthesis_stage_delegates_to_report_service():
    """SynthesisStage.execute should delegate to LocalSynthesisService."""
    from research_store.orchestrator import SynthesisStage

    mock_run_service = MagicMock()
    mock_uow_factory, mock_uow, _records = _make_mock_uow()
    mock_run_service.uow_factory = mock_uow_factory
    mock_run_service.evidence_service = MagicMock()
    mock_run_service.evidence_service.export_packet.return_value = deepcopy(
        _VALID_PACKET
    )

    mock_config = MagicMock()
    mock_config.embedding_model = "test-model"

    stage = SynthesisStage(run_service=mock_run_service, config=mock_config)

    # Pre-populate binding, draft, citation_pass as completed so only outline runs.
    run_id = UUID(_VALID_PACKET["run_id"])
    for sname in ("binding", "draft", "citation_pass"):
        mock_uow.synthesis_stages.update_synthesis_stage(
            {
                "id": str(uuid4()),
                "run_id": str(run_id),
                "stage_name": sname,
                "stage_status": "completed",
                "semantic_call_id": None,
                "semantic_artifact_id": None,
                "evidence_packet_revision": 1,
                "model_name": "test-model",
                "prompt_version": "v1",
                "schema_version": 1,
                "artifact": None,
                "error": None,
                "attempts": 1,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        )

    # Mock call_structured to succeed.
    mock_result = MagicMock()
    mock_result.error = None
    mock_result.value = {
        "schema_version": "synthesis-outline-v1",
        "run_id": str(run_id),
        "evidence_packet_revision": 2,
        "outline_sections": [],
        "unsupported_claims": [],
    }
    mock_result.semantic_call_id = str(uuid4())
    mock_result.artifact_ids = [str(uuid4())]

    with patch("model_gateway.call_structured", return_value=mock_result):
        result = stage.execute(
            run_id=run_id,
            run_revision=1,
            coverage_revision=2,
            run_state="synthesizing",
            context={},
        )

    # The orchestrator should have transitioned to "validating".
    assert result.stage == "synthesis"
    assert result.outcome.value == "continue"  # StageOutcome.CONTINUE


def test_synthesis_stage_handles_report_service_error():
    """SynthesisStage should handle ReportServiceError gracefully."""
    from research_store.orchestrator import SynthesisStage

    mock_run_service = MagicMock()
    mock_run_service.uow_factory = _make_mock_uow()[0]
    mock_run_service.evidence_service = MagicMock()
    mock_run_service.evidence_service.export_packet.return_value = deepcopy(
        _VALID_PACKET
    )

    mock_config = MagicMock()
    mock_config.embedding_model = "test-model"

    stage = SynthesisStage(run_service=mock_run_service, config=mock_config)

    # Mock call_structured to fail.
    mock_result = MagicMock()
    mock_result.error = "model timeout"
    mock_result.value = None
    mock_result.semantic_call_id = None
    mock_result.artifact_ids = []

    with patch("model_gateway.call_structured", return_value=mock_result):
        result = stage.execute(
            run_id=UUID(_VALID_PACKET["run_id"]),
            run_revision=1,
            coverage_revision=2,
            run_state="synthesizing",
            context={},
        )

    assert result.stage == "synthesis"
    # The orchestrator returns DEGRADED when synthesis partially fails.
    assert result.outcome.value == "degraded"


def test_synthesis_stage_wrong_state():
    """SynthesisStage should fail if run_state is not synthesizing."""
    from research_store.orchestrator import SynthesisStage

    mock_run_service = MagicMock()
    mock_config = MagicMock()

    stage = SynthesisStage(run_service=mock_run_service, config=mock_config)
    result = stage.execute(
        run_id=UUID(_VALID_PACKET["run_id"]),
        run_revision=1,
        coverage_revision=2,
        run_state="acquiring",
        context={},
    )

    assert result.stage == "synthesis"
    assert result.error is not None
    assert "requires synthesizing state" in result.error


# ---------------------------------------------------------------------------
# Deterministic unit tests (no network)
# ---------------------------------------------------------------------------


def test_service_creation():
    """Service should be creatable with mocked dependencies."""
    service, _, _, _ = _make_service()
    assert service.config is not None
    assert service._schemas is not None


def test_run_synthesis_packet_not_found():
    """If export_packet returns None, synthesis should fail."""
    service, mock_evidence, _, _ = _make_service()
    mock_evidence.export_packet.return_value = None

    with pytest.raises(ReportServiceError, match="EvidencePacket is None"):
        service.run_synthesis(
            run_id=UUID(_VALID_PACKET["run_id"]),
            packet_revision=1,
            model_name="test-model",
        )


def test_binding_stage_uses_injected_service():
    """Binding stage should use an injected ClaimBindingService when provided."""
    mock_binding = MagicMock()
    mock_binding.evaluate_claims.return_value = 5

    service, _, _, mock_uow = _make_service()
    service._binding_service = mock_binding

    run_id = UUID(_VALID_PACKET["run_id"])

    # Pre-populate all stages as completed except binding (which should run).
    for stage_name in SynthesisStageName:
        if stage_name.value == "binding":
            continue
        record = {
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
            "artifact": None,
            "error": None,
            "attempts": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        mock_uow.synthesis_stages.update_synthesis_stage(record)

    # Pre-populate binding as pending so it runs.
    binding_record = {
        "id": str(uuid4()),
        "run_id": str(run_id),
        "stage_name": "binding",
        "stage_status": "pending",
        "semantic_call_id": None,
        "semantic_artifact_id": None,
        "evidence_packet_revision": 1,
        "model_name": "test-model",
        "prompt_version": "v1",
        "schema_version": 1,
        "artifact": None,
        "error": None,
        "attempts": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    mock_uow.synthesis_stages.update_synthesis_stage(binding_record)

    summary = service.run_synthesis(
        run_id=run_id,
        packet_revision=1,
        model_name="test-model",
    )

    # The binding stage should have used the injected mock.
    # Note: packet_revision comes from the EvidencePacket's coverage_revision
    # which is 2 in _VALID_PACKET.
    mock_binding.evaluate_claims.assert_called_once_with(
        run_id=run_id,
        packet_revision=2,
        prompt_version="synthesis-v1",
        model_name="test-model",
        provider="local",
    )
    assert summary["stages"]["binding"]["status"] == "completed"
    assert summary["stages"]["binding"]["evidence_packet_revision"] == 5


def test_binding_stage_creates_default_service():
    """When no binding service is injected, a new ClaimBindingService is created."""
    service, _, _, _ = _make_service()

    # No binding service injected — should create one internally.
    assert service._binding_service is None

    run_id = UUID(_VALID_PACKET["run_id"])

    # Pre-populate all other stages as completed so only binding runs.
    with service.semantic.uow_factory() as uow:
        for stage_name in SynthesisStageName:
            if stage_name.value == "binding":
                continue
            record = {
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
                "artifact": None,
                "error": None,
                "attempts": 1,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
            uow.synthesis_stages.update_synthesis_stage(record)

    # Mock call_structured to return a valid claim-binding result.
    mock_result = MagicMock()
    mock_result.error = None
    mock_result.value = {
        "evaluations": [
            {
                "claim_id": _VALID_PACKET["claims"][0]["claim_id"],
                "semantic_status": "supported",
                "bindings": [
                    {
                        "passage_ids": [_VALID_PACKET["passages"][0]["passage_id"]],
                        "relationship": "supports",
                        "confidence": 0.95,
                        "uncertainty": "none",
                    }
                ],
            }
        ]
    }
    mock_result.semantic_call_id = str(uuid4())
    mock_result.artifact_ids = [str(uuid4())]

    with patch(
        "research_store.claim_binding_service.call_structured",
        return_value=mock_result,
    ):
        summary = service.run_synthesis(
            run_id=run_id,
            packet_revision=1,
            model_name="test-model",
        )

    # Binding should have completed (it created its own ClaimBindingService).
    assert summary["stages"]["binding"]["status"] == "completed"
    assert service._binding_service is not None


# ---------------------------------------------------------------------------
# Citation-pass stage tests
# ---------------------------------------------------------------------------


def test_citation_pass_stage_exercises_full_pipeline():
    """The citation_pass stage reads the draft artifact from synthesis_stages."""
    service, _, _, mock_uow = _make_service()
    run_id = UUID(_VALID_PACKET["run_id"])

    # Pre-populate outline, binding, and draft as completed so only
    # citation_pass runs.  The draft artifact must contain report_sections
    # so the citation_pass stage can read them from synthesis_stages.
    for sname in ("outline", "binding", "draft"):
        mock_uow.synthesis_stages.update_synthesis_stage(
            {
                "id": str(uuid4()),
                "run_id": str(run_id),
                "stage_name": sname,
                "stage_status": "completed",
                "semantic_call_id": None,
                "semantic_artifact_id": None,
                "evidence_packet_revision": 1,
                "model_name": "test-model",
                "prompt_version": "v1",
                "schema_version": 1,
                "artifact": (
                    {"report_sections": [{"section_id": "s1", "title": "Test"}]}
                    if sname == "draft"
                    else {}
                ),
                "error": None,
                "attempts": 1,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        )

    mock_result = MagicMock()
    mock_result.error = None
    mock_result.value = {
        "schema_version": "synthesis-citation-pass-v1",
        "run_id": str(run_id),
        "evidence_packet_revision": 2,
        "draft_revision": 1,
        "pass_status": "passed",
        "validation_results": [],
        "invented_citations": [],
        "unsupported_claims": [],
        "entailment_mismatches": [],
    }
    mock_result.semantic_call_id = str(uuid4())
    mock_result.artifact_ids = [str(uuid4())]

    with patch("model_gateway.call_structured", return_value=mock_result):
        summary = service.run_synthesis(
            run_id=run_id,
            packet_revision=2,
            model_name="test-model",
        )

    assert summary["stages"]["citation_pass"]["status"] == "completed"
    assert summary["overall_status"] == "completed"


def test_citation_pass_stage_reads_draft_from_synthesis_stages():
    """Citation_pass must read report_sections from synthesis_stages, not the packet."""
    service, _, _, mock_uow = _make_service()
    run_id = UUID(_VALID_PACKET["run_id"])

    # Pre-populate outline, binding, and draft as completed.
    for sname in ("outline", "binding", "draft"):
        mock_uow.synthesis_stages.update_synthesis_stage(
            {
                "id": str(uuid4()),
                "run_id": str(run_id),
                "stage_name": sname,
                "stage_status": "completed",
                "semantic_call_id": None,
                "semantic_artifact_id": None,
                "evidence_packet_revision": 1,
                "model_name": "test-model",
                "prompt_version": "v1",
                "schema_version": 1,
                "artifact": (
                    {"report_sections": [{"section_id": "s1", "title": "Test"}]}
                    if sname == "draft"
                    else {}
                ),
                "error": None,
                "attempts": 1,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        )

    captured_user_prompt = None

    def capture_call(*args, **kwargs):
        nonlocal captured_user_prompt
        captured_user_prompt = kwargs.get("user_prompt", "")
        mock_result = MagicMock()
        mock_result.error = None
        mock_result.value = {
            "schema_version": "synthesis-citation-pass-v1",
            "run_id": str(run_id),
            "evidence_packet_revision": 2,
            "draft_revision": 1,
            "pass_status": "passed",
            "validation_results": [],
            "invented_citations": [],
            "unsupported_claims": [],
            "entailment_mismatches": [],
        }
        mock_result.semantic_call_id = str(uuid4())
        mock_result.artifact_ids = [str(uuid4())]
        return mock_result

    with patch("model_gateway.call_structured", side_effect=capture_call):
        summary = service.run_synthesis(
            run_id=run_id,
            packet_revision=2,
            model_name="test-model",
        )

    import json

    prompt_data = json.loads(captured_user_prompt)
    # The draft_sections in the prompt must come from the draft artifact in
    # synthesis_stages, not from the EvidencePacket.
    assert len(prompt_data["draft_sections"]) == 1
    assert prompt_data["draft_sections"][0]["section_id"] == "s1"
    assert summary["stages"]["citation_pass"]["status"] == "completed"


# ---------------------------------------------------------------------------
# Draft stage data-flow tests
# ---------------------------------------------------------------------------


def test_draft_stage_reads_outline_from_synthesis_stages():
    """Draft must read outline_sections from synthesis_stages, not the packet."""
    service, _, _, mock_uow = _make_service()
    run_id = UUID(_VALID_PACKET["run_id"])

    # Pre-populate outline, binding, and citation_pass as completed so only
    # draft runs.
    mock_uow.synthesis_stages.update_synthesis_stage(
        {
            "id": str(uuid4()),
            "run_id": str(run_id),
            "stage_name": "outline",
            "stage_status": "completed",
            "semantic_call_id": None,
            "semantic_artifact_id": None,
            "evidence_packet_revision": 1,
            "model_name": "test-model",
            "prompt_version": "v1",
            "schema_version": 1,
            "artifact": {
                "outline_sections": [
                    {"section_id": "s1", "title": "Test Section", "claims": []}
                ]
            },
            "error": None,
            "attempts": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )
    mock_uow.synthesis_stages.update_synthesis_stage(
        {
            "id": str(uuid4()),
            "run_id": str(run_id),
            "stage_name": "binding",
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
    mock_uow.synthesis_stages.update_synthesis_stage(
        {
            "id": str(uuid4()),
            "run_id": str(run_id),
            "stage_name": "citation_pass",
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

    captured_user_prompt = None

    def capture_call(*args, **kwargs):
        nonlocal captured_user_prompt
        captured_user_prompt = kwargs.get("user_prompt", "")
        mock_result = MagicMock()
        mock_result.error = None
        mock_result.value = {
            "schema_version": "synthesis-draft-v1",
            "run_id": str(run_id),
            "evidence_packet_revision": 2,
            "report_sections": [],
            "unsupported_claims": [],
            "limitations": [],
        }
        mock_result.semantic_call_id = str(uuid4())
        mock_result.artifact_ids = [str(uuid4())]
        return mock_result

    with patch("model_gateway.call_structured", side_effect=capture_call):
        summary = service.run_synthesis(
            run_id=run_id,
            packet_revision=2,
            model_name="test-model",
        )

    import json

    prompt_data = json.loads(captured_user_prompt)
    # The outline_sections in the prompt must come from the outline artifact
    # in synthesis_stages, not from the EvidencePacket.
    assert len(prompt_data["outline_sections"]) == 1
    assert prompt_data["outline_sections"][0]["section_id"] == "s1"
    assert summary["stages"]["draft"]["status"] == "completed"


def test_draft_stage_handles_missing_outline_artifact():
    """If the outline artifact is missing, draft should proceed with empty outline."""
    service, _, _, mock_uow = _make_service()
    run_id = UUID(_VALID_PACKET["run_id"])

    # Pre-populate outline, binding, and citation_pass as completed but with
    # no artifact.
    mock_uow.synthesis_stages.update_synthesis_stage(
        {
            "id": str(uuid4()),
            "run_id": str(run_id),
            "stage_name": "outline",
            "stage_status": "completed",
            "semantic_call_id": None,
            "semantic_artifact_id": None,
            "evidence_packet_revision": 1,
            "model_name": "test-model",
            "prompt_version": "v1",
            "schema_version": 1,
            "artifact": None,
            "error": None,
            "attempts": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )
    mock_uow.synthesis_stages.update_synthesis_stage(
        {
            "id": str(uuid4()),
            "run_id": str(run_id),
            "stage_name": "binding",
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
    mock_uow.synthesis_stages.update_synthesis_stage(
        {
            "id": str(uuid4()),
            "run_id": str(run_id),
            "stage_name": "citation_pass",
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

    mock_result = MagicMock()
    mock_result.error = None
    mock_result.value = {
        "schema_version": "synthesis-draft-v1",
        "run_id": str(run_id),
        "evidence_packet_revision": 2,
        "report_sections": [],
        "unsupported_claims": [],
        "limitations": [],
    }
    mock_result.semantic_call_id = str(uuid4())
    mock_result.artifact_ids = [str(uuid4())]

    with patch("model_gateway.call_structured", return_value=mock_result):
        summary = service.run_synthesis(
            run_id=run_id,
            packet_revision=2,
            model_name="test-model",
        )

    # Should not raise — proceeds with empty outline_sections.
    assert summary["stages"]["draft"]["status"] == "completed"


def test_resume_failed_synthesis_logs_delegation():
    """resume_failed_synthesis should log and delegate to run_synthesis."""
    service, _, _, _ = _make_service()
    run_id = UUID(_VALID_PACKET["run_id"])

    with (
        patch.object(
            service, "run_synthesis", return_value={"overall_status": "completed"}
        ) as mock_run,
        patch("logging.Logger.info") as mock_info,
    ):
        service.resume_failed_synthesis(
            run_id=run_id,
            packet_revision=1,
            model_name="test-model",
        )

    mock_run.assert_called_once()
    mock_info.assert_called_once()
    # call_args is ((args), {kwargs}).
    # logger.info("resume ... %s (rev %d); ...", run_id, packet_revision)
    # args[0] = format string, args[1] = run_id (UUID), args[2] = packet_revision (int)
    assert isinstance(mock_info.call_args[0][2], int)
    assert mock_info.call_args[0][2] == 1


# ---------------------------------------------------------------------------
# Local endpoint outage and stale-packet-revision tests
# ---------------------------------------------------------------------------


def test_run_synthesis_outage_marks_failed_and_resume_succeeds():
    """A ConnectionError during outline should mark it failed; resume succeeds."""
    service, _mock_evidence, _mock_semantic, mock_uow = _make_service()
    run_id = UUID(_VALID_PACKET["run_id"])

    # Pre-populate outline as failed to simulate a prior outage.
    record = {
        "id": str(uuid4()),
        "run_id": str(run_id),
        "stage_name": "outline",
        "stage_status": "failed",
        "semantic_call_id": None,
        "semantic_artifact_id": None,
        "evidence_packet_revision": 1,
        "model_name": "test-model",
        "prompt_version": "v1",
        "schema_version": 1,
        "artifact": None,
        "error": "ConnectionError: model endpoint unavailable",
        "attempts": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    mock_uow.synthesis_stages.update_synthesis_stage(record)

    # Pre-populate binding as completed so it's skipped on resume.
    binding_record = {
        "id": str(uuid4()),
        "run_id": str(run_id),
        "stage_name": "binding",
        "stage_status": "completed",
        "semantic_call_id": None,
        "semantic_artifact_id": None,
        "evidence_packet_revision": 1,
        "model_name": "test-model",
        "prompt_version": "v1",
        "schema_version": 1,
        "artifact": {"new_packet_revision": 2},
        "error": None,
        "attempts": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    mock_uow.synthesis_stages.update_synthesis_stage(binding_record)

    # First call: simulate ConnectionError on outline retry.
    mock_error = MagicMock()
    mock_error.error = "ConnectionError: model endpoint unavailable"
    mock_error.value = None
    mock_error.semantic_call_id = None
    mock_error.artifact_ids = []

    with patch("model_gateway.call_structured", return_value=mock_error):
        summary = service.run_synthesis(
            run_id=run_id,
            packet_revision=1,
            model_name="test-model",
        )

    # Outline should be marked failed (attempt incremented).
    assert summary["stages"]["outline"]["status"] == "failed"
    assert summary["overall_status"] == "failed"

    # Second call: simulate successful retry after endpoint recovers.
    mock_result = MagicMock()
    mock_result.error = None
    mock_result.value = {
        "schema_version": "synthesis-outline-v1",
        "run_id": str(run_id),
        "evidence_packet_revision": 2,
        "outline_sections": [],
        "unsupported_claims": [],
    }
    mock_result.semantic_call_id = str(uuid4())
    mock_result.artifact_ids = [str(uuid4())]

    with patch("model_gateway.call_structured", return_value=mock_result):
        summary = service.resume_failed_synthesis(
            run_id=run_id,
            packet_revision=2,
            model_name="test-model",
        )

    # Outline should now be completed (retried after recovery).
    assert summary["stages"]["outline"]["status"] == "completed"
    assert summary["stages"]["binding"]["status"] == "skipped"
    assert summary["overall_status"] == "completed"


def test_run_synthesis_picks_up_new_packet_revision():
    """Changing EvidencePacket revision between calls is handled correctly."""
    mock_binding = MagicMock()
    mock_binding.evaluate_claims.return_value = 3

    service, mock_evidence, _, _ = _make_service()
    service._binding_service = mock_binding
    run_id = UUID(_VALID_PACKET["run_id"])

    # Create a second packet with more claims.
    packet_v2 = deepcopy(_VALID_PACKET)
    packet_v2["coverage_revision"] = 3
    packet_v2["claims"].append(
        {
            "claim_id": "00000000-0000-0000-0000-000000000103",
            "statement": "A second claim for revision 2.",
            "semantic_status": "supported",
            "uncertainty": "Independent replication is missing.",
        }
    )

    mock_evidence.export_packet.return_value = packet_v2

    # First call with revision 2 — should fail (model error) so we can test
    # that the second call with revision 3 picks up the new packet.
    mock_error = MagicMock()
    mock_error.error = "model timeout"
    mock_error.value = None
    mock_error.semantic_call_id = None
    mock_error.artifact_ids = []

    with patch("model_gateway.call_structured", return_value=mock_error):
        summary = service.run_synthesis(
            run_id=run_id,
            packet_revision=2,
            model_name="test-model",
        )

    assert summary["stages"]["outline"]["status"] == "failed"
    assert summary["overall_status"] == "failed"

    # Second call with revision 3 — should succeed and use the new packet.
    mock_result = MagicMock()
    mock_result.error = None
    mock_result.value = {
        "schema_version": "synthesis-outline-v1",
        "run_id": str(run_id),
        "evidence_packet_revision": 3,
        "outline_sections": [],
        "unsupported_claims": [],
    }
    mock_result.semantic_call_id = str(uuid4())
    mock_result.artifact_ids = [str(uuid4())]

    captured_prompts = []

    def capture_call(*args, **kwargs):
        nonlocal captured_prompts
        captured_prompts.append(kwargs.get("user_prompt", ""))
        return mock_result

    with patch("model_gateway.call_structured", side_effect=capture_call):
        summary = service.run_synthesis(
            run_id=run_id,
            packet_revision=3,
            model_name="test-model",
        )

    # Revision 3 should be picked up — outline should complete.
    assert summary["stages"]["outline"]["status"] == "completed"
    assert summary["stages"]["outline"]["evidence_packet_revision"] == 3
    # The first call is the outline stage — it should contain 2 claims
    # from the new packet (revision 3).
    import json

    prompt_data = json.loads(captured_prompts[0])
    assert len(prompt_data["claims"]) == 2
