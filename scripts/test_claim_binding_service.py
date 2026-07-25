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
        endpoint_alias="local",
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
            endpoint_alias="local",
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
            endpoint_alias="local",
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
        endpoint_alias="local",
        model_name="test-model",
    )

    persisted = service.evidence.persisted[0]
    assert len(persisted.claim_evidence_bindings) == 0
    assert persisted.claims[0].semantic_status == "unsupported"


import os

INTEGRATION_MARK = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY") and not os.environ.get("FIRECRAWL_LLM_LOCAL_BASE_URL"),
    reason="requires LLM endpoint",
)


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
            "uncertainty": "none"
        }
    ]
    mock_packet["passages"] = [
        {
            "passage_id": passage_id,
            "candidate_id": "00000000-0000-0000-0000-000000000301",
            "snapshot_id": "00000000-0000-0000-0000-000000000602",
            "chunk_id": "00000000-0000-0000-0000-000000000603",
            "text": "Paris is home to many famous landmarks, including the iconic Eiffel Tower, which was built in 1889.",
            "source_url": "https://example.com/paris"
        }
    ]

    new_rev = service.evaluate_claims(
        run_id=UUID(mock_packet["run_id"]),
        packet_revision=mock_packet["coverage_revision"],
        prompt_version="v1",
        endpoint_alias="local",
        model_name="chat",
    )

    persisted = service.evidence.persisted[0]
    assert len(persisted.claim_evidence_bindings) > 0
    binding = persisted.claim_evidence_bindings[0]
    assert str(binding.claim_id) == claim_id
    assert str(binding.passage_ids[0]) == passage_id
    assert binding.relationship == "supports"
    assert persisted.claims[0].semantic_status == "supported"

