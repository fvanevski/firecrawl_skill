"""Tests for semantic claim binding service."""

import json
from copy import deepcopy
from uuid import UUID

import pytest
from research_store.claim_binding_service import ClaimBindingService
from research_store.semantic_service import HostArtifactResult


class MockEvidenceService:
    def __init__(self, packet: dict):
        self.packet = packet
        self.persisted = []

    def export_packet(self, run_id, revision):
        return deepcopy(self.packet)

    def persist_packet(self, packet):
        self.persisted.append(packet)
        return packet.coverage_revision + 1


class MockSemanticCallService:
    def __init__(self):
        pass

    def uow_factory(self):
        class MockUOW:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            class runs:
                @staticmethod
                def get_run_status(run_id):
                    return {"lifecycle_revision": 1, "execution_mode": "agent_led"}

        return MockUOW()

    def start_model_call(self, *args, **kwargs):
        return "mock-call-id"

    def record_model_call(self, call_id, *args, **kwargs):
        pass

    def finish_model_call(self, call_id, *args, **kwargs):
        return []


@pytest.fixture
def mock_packet():
    with open("tests/fixtures/research_domain/valid.json") as f:
        data = json.load(f)
    return data["evidence-packet-v1"]


@pytest.fixture
def service(mock_packet):
    ev = MockEvidenceService(mock_packet)
    sem = MockSemanticCallService()
    return ClaimBindingService(sem, ev)


def test_evaluate_claims_success(service, mock_packet, monkeypatch):
    claim_id = mock_packet["claims"][0]["claim_id"]
    passage_id = mock_packet["passages"][0]["passage_id"]

    def mock_prompt(*args, **kwargs):
        return HostArtifactResult(
            value={
                "evaluations": [
                    {
                        "claim_id": claim_id,
                        "semantic_status": "supported",
                        "bindings": [
                            {
                                "passage_ids": [passage_id],
                                "relationship": "supports",
                                "confidence": 0.95,
                                "uncertainty": "none",
                            }
                        ],
                    }
                ]
            },
            provenance={},
            attempts=(),
        )

    monkeypatch.setattr(
        "research_store.claim_binding_service.call_structured", mock_prompt
    )

    new_rev = service.evaluate_claims(
        run_id=UUID(mock_packet["run_id"]),
        packet_revision=mock_packet["coverage_revision"],
        prompt_version="v1",
        model_name="test-model",
    )

    assert new_rev == mock_packet["coverage_revision"] + 1

    persisted = service.evidence.persisted[0]
    assert len(persisted.claim_evidence_bindings) == 1
    binding = persisted.claim_evidence_bindings[0]
    assert str(binding.claim_id) == claim_id
    assert str(binding.passage_ids[0]) == passage_id
    assert binding.relationship == "supports"
    assert binding.confidence == 0.95
    assert binding.model == "test-model"
    assert binding.prompt_version == "v1"
    assert binding.schema_version == 1
    assert binding.input_packet_revision == mock_packet["coverage_revision"]

    assert persisted.claims[0].semantic_status == "supported"


def test_evaluate_claims_rejects_invented_claim_id(service, mock_packet, monkeypatch):
    def mock_prompt(*args, **kwargs):
        return HostArtifactResult(
            value={
                "evaluations": [
                    {
                        "claim_id": "00000000-0000-0000-0000-000000009999",
                        "semantic_status": "supported",
                        "bindings": [],
                    }
                ]
            },
            provenance={},
            attempts=(),
        )

    monkeypatch.setattr(
        "research_store.claim_binding_service.call_structured", mock_prompt
    )

    with pytest.raises(ValueError, match="unknown claim IDs"):
        service.evaluate_claims(
            run_id=UUID(mock_packet["run_id"]),
            packet_revision=mock_packet["coverage_revision"],
            prompt_version="v1",
            model_name="test-model",
        )


def test_evaluate_claims_rejects_invented_passage_id(service, mock_packet, monkeypatch):
    claim_id = mock_packet["claims"][0]["claim_id"]

    def mock_prompt(*args, **kwargs):
        return HostArtifactResult(
            value={
                "evaluations": [
                    {
                        "claim_id": claim_id,
                        "semantic_status": "supported",
                        "bindings": [
                            {
                                "passage_ids": ["00000000-0000-0000-0000-000000009999"],
                                "relationship": "supports",
                                "confidence": 0.95,
                                "uncertainty": "none",
                            }
                        ],
                    }
                ]
            },
            provenance={},
            attempts=(),
        )

    monkeypatch.setattr(
        "research_store.claim_binding_service.call_structured", mock_prompt
    )

    with pytest.raises(ValueError, match="unknown passage IDs"):
        service.evaluate_claims(
            run_id=UUID(mock_packet["run_id"]),
            packet_revision=mock_packet["coverage_revision"],
            prompt_version="v1",
            model_name="test-model",
        )


def test_unsupported_claim_has_no_bindings(service, mock_packet, monkeypatch):
    claim_id = mock_packet["claims"][0]["claim_id"]

    def mock_prompt(*args, **kwargs):
        return HostArtifactResult(
            value={
                "evaluations": [
                    {
                        "claim_id": claim_id,
                        "semantic_status": "unsupported",
                        "bindings": [],
                    }
                ]
            },
            provenance={},
            attempts=(),
        )

    monkeypatch.setattr(
        "research_store.claim_binding_service.call_structured", mock_prompt
    )

    service.evaluate_claims(
        run_id=UUID(mock_packet["run_id"]),
        packet_revision=mock_packet["coverage_revision"],
        prompt_version="v1",
        model_name="test-model",
    )

    persisted = service.evidence.persisted[0]
    assert len(persisted.claim_evidence_bindings) == 0
    assert persisted.claims[0].semantic_status == "unsupported"


def test_no_claims_returns_same_revision(service, mock_packet, monkeypatch):
    """When the packet has no claims, evaluate_claims returns immediately."""
    mock_packet["claims"] = []

    new_rev = service.evaluate_claims(
        run_id=UUID(mock_packet["run_id"]),
        packet_revision=mock_packet["coverage_revision"],
        prompt_version="v1",
        model_name="test-model",
    )

    assert new_rev == mock_packet["coverage_revision"]


def test_call_structured_error_raises_runtime_error(service, mock_packet, monkeypatch):
    """When the LLM call fails, a RuntimeError is raised."""

    def mock_prompt(*args, **kwargs):
        return HostArtifactResult(
            value=None,
            provenance={},
            attempts=(),
            error="model timeout",
        )

    monkeypatch.setattr(
        "research_store.claim_binding_service.call_structured", mock_prompt
    )

    with pytest.raises(
        RuntimeError, match="Semantic claim binding failed: model timeout"
    ):
        service.evaluate_claims(
            run_id=UUID(mock_packet["run_id"]),
            packet_revision=mock_packet["coverage_revision"],
            prompt_version="v1",
            model_name="test-model",
        )


def test_missing_evaluations_key_produces_empty_bindings(
    service, mock_packet, monkeypatch
):
    """When the model returns no evaluations key, the service produces a valid packet with no bindings."""

    def mock_prompt(*args, **kwargs):
        return HostArtifactResult(
            value={},
            provenance={},
            attempts=(),
        )

    monkeypatch.setattr(
        "research_store.claim_binding_service.call_structured", mock_prompt
    )

    new_rev = service.evaluate_claims(
        run_id=UUID(mock_packet["run_id"]),
        packet_revision=mock_packet["coverage_revision"],
        prompt_version="v1",
        model_name="test-model",
    )

    assert new_rev == mock_packet["coverage_revision"] + 1
    persisted = service.evidence.persisted[0]
    assert len(persisted.claim_evidence_bindings) == 0


def test_multiple_bindings_per_claim(service, mock_packet, monkeypatch):
    """A single claim can have multiple bindings with different relationships."""
    claim_id = mock_packet["claims"][0]["claim_id"]
    passage_id = mock_packet["passages"][0]["passage_id"]

    def mock_prompt(*args, **kwargs):
        return HostArtifactResult(
            value={
                "evaluations": [
                    {
                        "claim_id": claim_id,
                        "semantic_status": "qualified",
                        "bindings": [
                            {
                                "passage_ids": [passage_id],
                                "relationship": "supports",
                                "confidence": 0.9,
                                "uncertainty": "partial support",
                            },
                            {
                                "passage_ids": [passage_id],
                                "relationship": "qualifies",
                                "confidence": 0.6,
                                "uncertainty": "context limits applicability",
                            },
                        ],
                    }
                ]
            },
            provenance={},
            attempts=(),
        )

    monkeypatch.setattr(
        "research_store.claim_binding_service.call_structured", mock_prompt
    )

    service.evaluate_claims(
        run_id=UUID(mock_packet["run_id"]),
        packet_revision=mock_packet["coverage_revision"],
        prompt_version="v1",
        model_name="test-model",
    )

    persisted = service.evidence.persisted[0]
    assert len(persisted.claim_evidence_bindings) == 2
    relationships = {b.relationship for b in persisted.claim_evidence_bindings}
    assert "supports" in relationships
    assert "qualifies" in relationships
    assert persisted.claims[0].semantic_status == "qualified"


def test_missing_packet_raises_value_error(mock_packet, monkeypatch):
    """When export_packet returns None, a ValueError is raised."""

    class NoneEvidenceService:
        def export_packet(self, run_id, revision):
            return None

        def persist_packet(self, packet):
            return 1

    class MockSemanticCallService:
        def uow_factory(self):
            class MockUOW:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                class runs:
                    @staticmethod
                    def get_run_status(run_id):
                        return {"lifecycle_revision": 1, "execution_mode": "agent_led"}

            return MockUOW()

        def start_model_call(self, *args, **kwargs):
            return "mock-call-id"

        def record_model_call(self, call_id, *args, **kwargs):
            pass

        def finish_model_call(self, call_id, *args, **kwargs):
            return []

    svc = ClaimBindingService(MockSemanticCallService(), NoneEvidenceService())

    with pytest.raises(ValueError, match="not found"):
        svc.evaluate_claims(
            run_id=UUID(mock_packet["run_id"]),
            packet_revision=mock_packet["coverage_revision"],
            prompt_version="v1",
            model_name="test-model",
        )


def test_binding_ids_are_unique(service, mock_packet, monkeypatch):
    """Each binding gets a unique ID via uuid4()."""
    claim_id = mock_packet["claims"][0]["claim_id"]
    passage_id = mock_packet["passages"][0]["passage_id"]

    def mock_prompt(*args, **kwargs):
        return HostArtifactResult(
            value={
                "evaluations": [
                    {
                        "claim_id": claim_id,
                        "semantic_status": "supported",
                        "bindings": [
                            {
                                "passage_ids": [passage_id],
                                "relationship": "supports",
                                "confidence": 0.95,
                                "uncertainty": "none",
                            }
                        ],
                    }
                ]
            },
            provenance={},
            attempts=(),
        )

    monkeypatch.setattr(
        "research_store.claim_binding_service.call_structured", mock_prompt
    )

    service.evaluate_claims(
        run_id=UUID(mock_packet["run_id"]),
        packet_revision=mock_packet["coverage_revision"],
        prompt_version="v1",
        model_name="test-model",
    )

    persisted = service.evidence.persisted[0]
    binding_ids = [str(b.binding_id) for b in persisted.claim_evidence_bindings]
    assert len(binding_ids) == len(set(binding_ids))


import os

INTEGRATION_MARK = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY")
    and not os.environ.get("FIRECRAWL_LLM_LOCAL_BASE_URL"),
    reason="requires LLM endpoint",
)


def test_invalid_semantic_status_raises_value_error(service, mock_packet, monkeypatch):
    """Invalid semantic_status values are rejected before any bindings are created."""
    claim_id = mock_packet["claims"][0]["claim_id"]

    def mock_prompt(*args, **kwargs):
        return HostArtifactResult(
            value={
                "evaluations": [
                    {
                        "claim_id": claim_id,
                        "semantic_status": "supportive",  # invalid value
                        "bindings": [],
                    }
                ]
            },
            provenance={},
            attempts=(),
        )

    monkeypatch.setattr(
        "research_store.claim_binding_service.call_structured", mock_prompt
    )

    with pytest.raises(ValueError, match="invalid semantic_status"):
        service.evaluate_claims(
            run_id=UUID(mock_packet["run_id"]),
            packet_revision=mock_packet["coverage_revision"],
            prompt_version="v1",
            model_name="test-model",
        )


def test_partial_evaluation_failure_no_partial_bindings(
    service, mock_packet, monkeypatch
):
    """When an invalid claim ID appears later, no bindings are created."""
    claim_id = mock_packet["claims"][0]["claim_id"]
    passage_id = mock_packet["passages"][0]["passage_id"]

    def mock_prompt(*args, **kwargs):
        return HostArtifactResult(
            value={
                "evaluations": [
                    {
                        "claim_id": claim_id,
                        "semantic_status": "supported",
                        "bindings": [
                            {
                                "passage_ids": [passage_id],
                                "relationship": "supports",
                                "confidence": 0.95,
                                "uncertainty": "none",
                            }
                        ],
                    },
                    {
                        "claim_id": "00000000-0000-0000-0000-000000009999",  # invalid
                        "semantic_status": "supported",
                        "bindings": [],
                    },
                ]
            },
            provenance={},
            attempts=(),
        )

    monkeypatch.setattr(
        "research_store.claim_binding_service.call_structured", mock_prompt
    )

    with pytest.raises(ValueError, match="unknown claim IDs"):
        service.evaluate_claims(
            run_id=UUID(mock_packet["run_id"]),
            packet_revision=mock_packet["coverage_revision"],
            prompt_version="v1",
            model_name="test-model",
        )

    # No bindings should have been persisted
    assert len(service.evidence.persisted) == 0


@INTEGRATION_MARK
def test_evaluate_claims_integration(service, mock_packet):
    # Setup some specific real claims and passages
    claim_id = mock_packet["claims"][0]["claim_id"]
    passage_id = mock_packet["passages"][0]["passage_id"]

    mock_packet["claims"] = [
        {
            "claim_id": claim_id,
            "statement": "The Eiffel Tower is located in Paris.",
            "semantic_status": "unassessed",
            "uncertainty": "none",
        }
    ]
    mock_packet["passages"] = [
        {
            "passage_id": passage_id,
            "candidate_id": "00000000-0000-0000-0000-000000000301",
            "snapshot_id": "00000000-0000-0000-0000-000000000602",
            "chunk_id": "00000000-0000-0000-0000-000000000603",
            "text": "Paris is home to many famous landmarks, including the iconic Eiffel Tower, which was built in 1889.",
            "source_url": "https://example.com/paris",
        }
    ]

    service.evaluate_claims(
        run_id=UUID(mock_packet["run_id"]),
        packet_revision=mock_packet["coverage_revision"],
        prompt_version="v1",
        model_name="chat",
    )

    persisted = service.evidence.persisted[0]
    assert len(persisted.claim_evidence_bindings) > 0
    binding = persisted.claim_evidence_bindings[0]
    assert str(binding.claim_id) == claim_id
    assert str(binding.passage_ids[0]) == passage_id
    assert binding.relationship == "supports"
    assert persisted.claims[0].semantic_status == "supported"
