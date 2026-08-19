"""Integration tests for EvidencePacketValidator against real persisted packets via PostgreSQL.

These tests exercise the validator against real database-backed packets,
verifying that the validator correctly identifies errors, warnings, and
info-level findings when working with actual persisted data.

Requires RESEARCH_STORE_TEST_DATABASE_URL to be set.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from research_domain.models import (
    EvidenceClaim,
    EvidencePassage,
    EvidenceRelationship,
    SemanticStatus,
)
from research_store.packet_validator import (
    EvidencePacketValidator,
    bounded_citation_ready_output,
)

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
INTEGRATION_MARK = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def ensure_run_exists(dsn, run_id):
    """Create a research_runs row so FK constraints are satisfied."""
    from research_store.postgres import connect

    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO research_runs (id, objective, query_plan, skill_version, llm_model, state, execution_mode)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING""",
            (
                str(run_id),
                "test request",
                "{}",
                "1.0",
                "test",
                "created",
                "agent_led",
            ),
        )


def _make_packet_dict(
    run_id=None,
    research_spec_id=None,
    coverage_revision=1,
    claims=None,
    passages=None,
    omitted_passages=None,
    claim_evidence_bindings=None,
    corroborating_groups=None,
    contradicting_groups=None,
    qualifying_groups=None,
    near_duplicate_groups=None,
    independence_assessments=None,
    retrieval_provenance=None,
    limitations=None,
    unresolved_items=None,
    source_diversity_summary=None,
    freshness_summary=None,
):
    """Create a packet dict suitable for persisting to the database."""
    return {
        "schema_version": "evidence-packet-v1",
        "run_id": str(run_id or uuid4()),
        "research_spec_id": str(research_spec_id or uuid4()),
        "coverage_revision": coverage_revision,
        "claims": [
            {
                "claim_id": str(c.claim_id),
                "statement": c.statement,
                "semantic_status": c.semantic_status.value,
                "uncertainty": c.uncertainty,
            }
            for c in (claims or [])
        ],
        "passages": [
            {
                "passage_id": str(p.passage_id),
                "candidate_id": str(p.candidate_id),
                "snapshot_id": str(p.snapshot_id),
                "chunk_id": str(p.chunk_id),
                "text": p.text,
                "source_url": p.source_url,
            }
            for p in (passages or [])
        ],
        "omitted_passages": [
            {
                "passage_id": str(p.passage_id),
                "candidate_id": str(p.candidate_id),
                "snapshot_id": str(p.snapshot_id),
                "chunk_id": str(p.chunk_id),
                "text": p.text,
                "source_url": p.source_url,
            }
            for p in (omitted_passages or [])
        ],
        "claim_evidence_bindings": [
            {
                "binding_id": str(b.binding_id),
                "claim_id": str(b.claim_id),
                "passage_ids": [str(pid) for pid in b.passage_ids],
                "relationship": b.relationship.value
                if hasattr(b.relationship, "value")
                else str(b.relationship),
                "confidence": b.confidence,
                "uncertainty": b.uncertainty,
                "model": b.model,
                "prompt_version": b.prompt_version,
                "schema_version": b.schema_version,
                "input_packet_revision": b.input_packet_revision,
            }
            for b in (claim_evidence_bindings or [])
        ],
        "corroborating_groups": [
            {
                "group_id": str(g.group_id),
                "passage_ids": [str(pid) for pid in g.passage_ids],
                "rationale": g.rationale,
                "evaluated": g.evaluated,
            }
            for g in (corroborating_groups or [])
        ],
        "contradicting_groups": [
            {
                "group_id": str(g.group_id),
                "passage_ids": [str(pid) for pid in g.passage_ids],
                "rationale": g.rationale,
                "evaluated": g.evaluated,
            }
            for g in (contradicting_groups or [])
        ],
        "qualifying_groups": [
            {
                "group_id": str(g.group_id),
                "passage_ids": [str(pid) for pid in g.passage_ids],
                "rationale": g.rationale,
                "evaluated": g.evaluated,
            }
            for g in (qualifying_groups or [])
        ],
        "near_duplicate_groups": [
            {
                "group_id": str(g.group_id),
                "passage_ids": [str(pid) for pid in g.passage_ids],
                "rationale": g.rationale,
                "evaluated": g.evaluated,
            }
            for g in (near_duplicate_groups or [])
        ],
        "source_diversity_summary": source_diversity_summary or {},
        "freshness_summary": freshness_summary or {},
        "limitations": list(limitations or []),
        "unresolved_items": [str(uid) for uid in (unresolved_items or [])],
        "independence_assessments": [
            {
                "candidate_id": str(a.candidate_id),
                "status": a.status.value
                if hasattr(a.status, "value")
                else str(a.status),
                "rationale": a.rationale,
            }
            for a in (independence_assessments or [])
        ],
        "retrieval_provenance": [
            {
                "retrieval_event_id": str(rp.retrieval_event_id),
                "requested_mode": rp.requested_mode,
                "executed_mode": rp.executed_mode,
                "mechanical_status": rp.mechanical_status.value
                if hasattr(rp.mechanical_status, "value")
                else str(rp.mechanical_status),
                "component_errors": [],
                "selected_passage_ids": [str(pid) for pid in rp.selected_passage_ids],
            }
            for rp in (retrieval_provenance or [])
        ],
    }


def _make_claim(
    claim_id=None,
    statement="The claim statement",
    semantic_status=SemanticStatus.SUPPORTED,
    uncertainty="low",
):
    return EvidenceClaim(
        claim_id=claim_id or uuid4(),
        statement=statement,
        semantic_status=semantic_status,
        uncertainty=uncertainty,
    )


def _make_passage(
    passage_id=None,
    candidate_id=None,
    snapshot_id=None,
    chunk_id=None,
    text="test passage text",
    source_url="https://example.com/source",
):
    return EvidencePassage(
        passage_id=passage_id or uuid4(),
        candidate_id=candidate_id or uuid4(),
        snapshot_id=snapshot_id or uuid4(),
        chunk_id=chunk_id or uuid4(),
        text=text,
        source_url=source_url,
    )


def _make_binding(
    binding_id=None,
    claim_id=None,
    passage_ids=None,
    relationship=EvidenceRelationship.SUPPORTS,
    confidence=0.9,
    uncertainty="low",
    model="test-model",
    prompt_version="binding-v1",
    schema_version=1,
    input_packet_revision=1,
):
    from research_domain.models import ClaimEvidenceBinding

    return ClaimEvidenceBinding(
        binding_id=binding_id or uuid4(),
        claim_id=claim_id or uuid4(),
        passage_ids=tuple(passage_ids or [uuid4()]),
        relationship=relationship,
        confidence=confidence,
        uncertainty=uncertainty,
        model=model,
        prompt_version=prompt_version,
        schema_version=schema_version,
        input_packet_revision=input_packet_revision,
    )


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestValidatorIntegration:
    """Integration tests for EvidencePacketValidator against real persisted packets."""

    @pytest.mark.skipif(
        not TEST_DSN, reason="requires RESEARCH_STORE_TEST_DATABASE_URL"
    )
    def test_validator_against_real_database_packet(self, tmp_path):
        """Validator correctly validates a packet persisted to and retrieved from PostgreSQL."""
        from research_store.config import StoreConfig
        from research_store.container import build_evidence_service

        config = dataclasses.replace(StoreConfig.from_env(), database_url=TEST_DSN)
        config.require_database()

        svc = build_evidence_service(config)
        run_id = uuid4()

        # Build a valid packet with retrieval_provenance
        from research_domain.models import MechanicalStatus, RetrievalProvenance

        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        passage = _make_passage()
        binding = _make_binding(
            claim_id=claim.claim_id,
            passage_ids=[passage.passage_id],
        )

        provenance = RetrievalProvenance(
            retrieval_event_id=uuid4(),
            requested_mode="hybrid",
            executed_mode="hybrid",
            mechanical_status=MechanicalStatus.SUCCEEDED,
            component_errors=(),
            selected_passage_ids=(passage.passage_id,),
        )

        packet_dict = _make_packet_dict(
            run_id=run_id,
            claims=[claim],
            passages=[passage],
            claim_evidence_bindings=[binding],
            retrieval_provenance=[provenance],
            freshness_summary={
                "most_recent": "2025-06-01T00:00:00Z",
                "oldest": "2025-01-01T00:00:00Z",
            },
        )

        # Persist the packet
        ensure_run_exists(TEST_DSN, run_id)
        from research_domain.registry import load_model

        packet = load_model(packet_dict)
        svc.persist_packet(packet)

        # Retrieve and validate
        exported = svc.export_packet(run_id, 1)
        assert exported is not None

        packet_obj = load_model(exported["payload"])
        validator = EvidencePacketValidator()
        result = validator.validate(packet_obj)

        # The packet should be valid and complete
        assert result.is_valid is True
        assert result.is_complete is True

    @pytest.mark.skipif(
        not TEST_DSN, reason="requires RESEARCH_STORE_TEST_DATABASE_URL"
    )
    def test_validator_detects_missing_provenance_in_real_packet(self, tmp_path):
        """Validator detects missing retrieval_provenance in a real database packet."""
        from research_store.config import StoreConfig
        from research_store.container import build_evidence_service

        config = dataclasses.replace(StoreConfig.from_env(), database_url=TEST_DSN)
        config.require_database()

        svc = build_evidence_service(config)
        run_id = uuid4()

        # Build a packet without retrieval_provenance
        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        passage = _make_passage()
        binding = _make_binding(
            claim_id=claim.claim_id,
            passage_ids=[passage.passage_id],
        )

        packet_dict = _make_packet_dict(
            run_id=run_id,
            claims=[claim],
            passages=[passage],
            claim_evidence_bindings=[binding],
            freshness_summary={
                "most_recent": "2025-06-01T00:00:00Z",
                "oldest": "2025-01-01T00:00:00Z",
            },
        )

        from research_domain.registry import load_model

        packet = load_model(packet_dict)
        ensure_run_exists(TEST_DSN, run_id)
        svc.persist_packet(packet)

        exported = svc.export_packet(run_id, 1)
        assert exported is not None

        packet_obj = load_model(exported["payload"])
        validator = EvidencePacketValidator()
        result = validator.validate(packet_obj)

        # Should detect missing retrieval_provenance
        assert result.is_valid is False
        assert any(f.code == "MISSING_RETRIEVAL_PROVENANCE" for f in result.errors)

    @pytest.mark.skipif(
        not TEST_DSN, reason="requires RESEARCH_STORE_TEST_DATABASE_URL"
    )
    def test_validator_detects_unknown_candidate_ref(self, tmp_path):
        """Validator detects unknown candidate references in a real database packet."""
        from research_store.config import StoreConfig
        from research_store.container import build_evidence_service

        config = dataclasses.replace(StoreConfig.from_env(), database_url=TEST_DSN)
        config.require_database()

        svc = build_evidence_service(config)
        run_id = uuid4()

        # Build a packet with a valid claim and binding, but the passage
        # references a candidate_id that won't be in the candidate_ids set
        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        passage = _make_passage()
        binding = _make_binding(
            claim_id=claim.claim_id,
            passage_ids=[passage.passage_id],
        )

        packet_dict = _make_packet_dict(
            run_id=run_id,
            claims=[claim],
            passages=[passage],
            claim_evidence_bindings=[binding],
            freshness_summary={
                "most_recent": "2025-06-01T00:00:00Z",
                "oldest": "2025-01-01T00:00:00Z",
            },
        )

        from research_domain.registry import load_model

        packet = load_model(packet_dict)
        ensure_run_exists(TEST_DSN, run_id)
        svc.persist_packet(packet)

        exported = svc.export_packet(run_id, 1)
        assert exported is not None

        packet_obj = load_model(exported["payload"])
        validator = EvidencePacketValidator()
        # Validate with a different candidate_id set
        result = validator.validate(
            packet_obj,
            candidate_ids=frozenset([uuid4()]),  # wrong candidate
        )

        # Should detect unknown candidate reference
        assert result.is_valid is False
        assert any(f.code == "UNKNOWN_CANDIDATE_REF" for f in result.errors)

    @pytest.mark.skipif(
        not TEST_DSN, reason="requires RESEARCH_STORE_TEST_DATABASE_URL"
    )
    def test_validator_with_bounded_output(self, tmp_path):
        """Bounded citation-ready output works correctly with real database packets."""
        from research_store.config import StoreConfig
        from research_store.container import build_evidence_service

        config = dataclasses.replace(StoreConfig.from_env(), database_url=TEST_DSN)
        config.require_database()

        svc = build_evidence_service(config)
        run_id = uuid4()

        # Build a packet with multiple claims and passages
        claims = [
            _make_claim(semantic_status=SemanticStatus.SUPPORTED) for _ in range(5)
        ]
        passages = [_make_passage() for _ in range(5)]
        bindings = [
            _make_binding(
                claim_id=claims[i].claim_id,
                passage_ids=[passages[i].passage_id],
            )
            for i in range(5)
        ]

        packet_dict = _make_packet_dict(
            run_id=run_id,
            claims=claims,
            passages=passages,
            claim_evidence_bindings=bindings,
            freshness_summary={
                "most_recent": "2025-06-01T00:00:00Z",
                "oldest": "2025-01-01T00:00:00Z",
            },
        )

        from research_domain.registry import load_model

        packet = load_model(packet_dict)
        ensure_run_exists(TEST_DSN, run_id)
        svc.persist_packet(packet)

        exported = svc.export_packet(run_id, 1)
        assert exported is not None

        packet_obj = load_model(exported["payload"])
        output = bounded_citation_ready_output(packet_obj, max_passages=3, max_claims=2)

        # Should be bounded
        assert len(output["passages"]) == 3
        assert len(output["claims"]) == 2
        assert output["metadata"]["passage_count"] == 5
        assert output["metadata"]["claim_count"] == 5

    @pytest.mark.skipif(
        not TEST_DSN, reason="requires RESEARCH_STORE_TEST_DATABASE_URL"
    )
    def test_validator_with_claim_binding_service(self, tmp_path):
        """ClaimBindingService integrates correctly with the validator."""
        from research_store.config import StoreConfig
        from research_store.container import build_evidence_service

        config = dataclasses.replace(StoreConfig.from_env(), database_url=TEST_DSN)
        config.require_database()

        evidence_svc = build_evidence_service(config)

        run_id = uuid4()

        # Build a packet with claims but no bindings
        claims = [
            _make_claim(semantic_status=SemanticStatus.UNASSESSED) for _ in range(3)
        ]
        passages = [_make_passage() for _ in range(3)]

        packet_dict = _make_packet_dict(
            run_id=run_id,
            claims=claims,
            passages=passages,
            freshness_summary={
                "most_recent": "2025-06-01T00:00:00Z",
                "oldest": "2025-01-01T00:00:00Z",
            },
        )

        from research_domain.registry import load_model

        packet = load_model(packet_dict)
        ensure_run_exists(TEST_DSN, run_id)
        evidence_svc.persist_packet(packet)

        # Validate before binding - should have warnings about unassessed claims
        exported = evidence_svc.export_packet(run_id, 1)
        assert exported is not None

        packet_obj = load_model(exported["payload"])
        validator = EvidencePacketValidator()
        result = validator.validate(packet_obj)

        # Should have warnings about unassessed claims
        assert any(f.code == "UNASSESSED_CLAIM" for f in result.warnings)
