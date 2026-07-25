"""Tests for semantic claim binding service."""

import json
import pytest
from copy import deepcopy
from uuid import UUID, uuid4

from research_store.semantic_service import HostArtifactResult
from research_store.claim_binding_service import ClaimBindingService

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
            def __enter__(self): return self
            def __exit__(self, *args): pass
            class runs:
                @staticmethod
                def get_run_status(run_id):
                    return {"lifecycle_revision": 1, "execution_mode": "agent_led"}
        return MockUOW()


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
                                "uncertainty": "none"
                            }
                        ]
                    }
                ]
            },
            provenance={},
            attempts=()
        )
        
    monkeypatch.setattr("research_store.claim_binding_service.call_structured", mock_prompt)
    
    new_rev = service.evaluate_claims(
        run_id=UUID(mock_packet["run_id"]),
        packet_revision=mock_packet["coverage_revision"],
        prompt_version="v1",
        endpoint_alias="local",
        model_name="test-model"
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
                        "bindings": []
                    }
                ]
            },
            provenance={},
            attempts=()
        )
        
    monkeypatch.setattr("research_store.claim_binding_service.call_structured", mock_prompt)
    
    with pytest.raises(ValueError, match="unknown claim IDs"):
        service.evaluate_claims(
            run_id=UUID(mock_packet["run_id"]),
            packet_revision=mock_packet["coverage_revision"],
            prompt_version="v1",
            endpoint_alias="local",
            model_name="test-model"
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
                                "uncertainty": "none"
                            }
                        ]
                    }
                ]
            },
            provenance={},
            attempts=()
        )
        
    monkeypatch.setattr("research_store.claim_binding_service.call_structured", mock_prompt)
    
    with pytest.raises(ValueError, match="unknown passage IDs"):
        service.evaluate_claims(
            run_id=UUID(mock_packet["run_id"]),
            packet_revision=mock_packet["coverage_revision"],
            prompt_version="v1",
            endpoint_alias="local",
            model_name="test-model"
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
                        "bindings": []
                    }
                ]
            },
            provenance={},
            attempts=()
        )
        
    monkeypatch.setattr("research_store.claim_binding_service.call_structured", mock_prompt)
    
    service.evaluate_claims(
        run_id=UUID(mock_packet["run_id"]),
        packet_revision=mock_packet["coverage_revision"],
        prompt_version="v1",
        endpoint_alias="local",
        model_name="test-model"
    )
    
    persisted = service.evidence.persisted[0]
    assert len(persisted.claim_evidence_bindings) == 0
    assert persisted.claims[0].semantic_status == "unsupported"
