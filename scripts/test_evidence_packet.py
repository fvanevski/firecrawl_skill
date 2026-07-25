"""Tests for deterministic EvidencePacket foundation (issue #54).

Covers:
- Exact provenance.
- Token limits enforced deterministically.
- Empty semantic groups marked unevaluated.
- Source diversity and freshness summaries.
- Revision immutability.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from budget_policy import DEFAULT_POLICY, ResourceCaps
from research_store.evidence import EvidenceService

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
INTEGRATION_MARK = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


def _make_candidate(
    candidate_id=None,
    snapshot_id=None,
    chunk_id=None,
    text="Some excerpt text",
    url="https://example.com/source1",
    date="2025-01-01T12:00:00Z",
):
    return {
        "candidate_id": str(candidate_id or uuid4()),
        "snapshot_id": str(snapshot_id or uuid4()),
        "chunk_id": str(chunk_id or uuid4()),
        "text": text,
        "url": url,
        "date": date,
    }


def test_build_evidence_packet_deterministic_ordering_and_summaries():
    svc = EvidenceService(lambda: None, budget_policy=DEFAULT_POLICY)

    # Intentionally out of order candidates
    c1 = _make_candidate(
        snapshot_id="b0000000-0000-0000-0000-000000000000",
        url="https://a.com",
        date="2024-01-01T00:00:00Z",
    )
    c2 = _make_candidate(
        snapshot_id="a0000000-0000-0000-0000-000000000000",
        url="https://b.com",
        date="2025-01-01T00:00:00Z",
    )

    # Caps large enough to fit both
    caps = ResourceCaps.from_mapping(
        {
            **DEFAULT_POLICY.profiles["standard"].to_dict(),
            "max_evidence_packet_tokens": 8000,
        }
    )

    packet = svc.build_evidence_packet(
        run_id=uuid4(),
        research_spec_id=uuid4(),
        coverage_revision=1,
        candidates=[c1, c2],
        retrieval_events=[],
        effective_caps=caps,
    )

    # Check ordering
    assert str(packet.passages[0].snapshot_id) == "a0000000-0000-0000-0000-000000000000"
    assert str(packet.passages[1].snapshot_id) == "b0000000-0000-0000-0000-000000000000"

    # Check summaries
    assert packet.source_diversity_summary["unique_sources"] == 2
    assert packet.source_diversity_summary["sources"] == [
        "https://a.com",
        "https://b.com",
    ]
    assert packet.freshness_summary["oldest"] == "2024-01-01T00:00:00+00:00"
    assert packet.freshness_summary["most_recent"] == "2025-01-01T00:00:00+00:00"


def test_build_evidence_packet_token_limits_enforced():
    svc = EvidenceService(lambda: None, budget_policy=DEFAULT_POLICY)

    c1 = _make_candidate(text="Word " * 10)
    c2 = _make_candidate(text="Word " * 100)

    # c1 is ~10 tokens. c2 is ~100. Set cap to 50 to fit only c1.
    caps = ResourceCaps.from_mapping(
        {
            **DEFAULT_POLICY.profiles["standard"].to_dict(),
            "max_evidence_packet_tokens": 50,
        }
    )

    packet = svc.build_evidence_packet(
        run_id=uuid4(),
        research_spec_id=uuid4(),
        coverage_revision=1,
        candidates=[
            c1,
            c2,
        ],  # Note: sorted by snapshot_id inside, we can just check length
        retrieval_events=[],
        effective_caps=caps,
    )

    # Only 1 passage should be included due to limits
    assert len(packet.passages) == 1
    assert len(packet.omitted_passages) == 1

    # Near duplicate group should have the omitted candidates
    assert len(packet.near_duplicate_groups) == 1
    group = packet.near_duplicate_groups[0]
    assert group.rationale == "omitted_due_to_budget"
    assert not group.evaluated
    assert len(group.passage_ids) == 1
    assert group.passage_ids[0] == packet.omitted_passages[0].passage_id


def test_build_evidence_packet_empty_candidates():
    """Empty candidate list produces a valid empty packet with zero summaries."""
    svc = EvidenceService(lambda: None, budget_policy=DEFAULT_POLICY)

    caps = ResourceCaps.from_mapping(
        {
            **DEFAULT_POLICY.profiles["standard"].to_dict(),
            "max_evidence_packet_tokens": 8000,
        }
    )

    packet = svc.build_evidence_packet(
        run_id=uuid4(),
        research_spec_id=uuid4(),
        coverage_revision=1,
        candidates=[],
        retrieval_events=[],
        effective_caps=caps,
    )

    assert len(packet.passages) == 0
    assert len(packet.omitted_passages) == 0
    assert packet.source_diversity_summary["unique_sources"] == 0
    assert packet.source_diversity_summary["sources"] == []
    assert packet.freshness_summary["most_recent"] is None
    assert packet.freshness_summary["oldest"] is None
    assert len(packet.near_duplicate_groups) == 0


def test_build_evidence_packet_zero_budget_all_omitted():
    """Zero budget cap puts every candidate in omitted_passages."""
    svc = EvidenceService(lambda: None, budget_policy=DEFAULT_POLICY)

    c1 = _make_candidate(text="Word " * 10)
    c2 = _make_candidate(text="Word " * 20)

    caps = ResourceCaps.from_mapping(
        {
            **DEFAULT_POLICY.profiles["standard"].to_dict(),
            "max_evidence_packet_tokens": 0,
        }
    )

    packet = svc.build_evidence_packet(
        run_id=uuid4(),
        research_spec_id=uuid4(),
        coverage_revision=1,
        candidates=[c1, c2],
        retrieval_events=[],
        effective_caps=caps,
    )

    assert len(packet.passages) == 0
    assert len(packet.omitted_passages) == 2
    assert len(packet.near_duplicate_groups) == 1
    group = packet.near_duplicate_groups[0]
    assert group.rationale == "omitted_due_to_budget"
    assert not group.evaluated
    assert len(group.passage_ids) == 2


def ensure_run_exists(dsn, run_id):
    from research_store.postgres import connect

    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO research_runs (id, original_request, query_plan, skill_version, llm_model, status, state, execution_mode, objective)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING""",
            (
                str(run_id),
                "test request",
                "{}",
                "1.0",
                "test",
                "running",
                "created",
                "agent_led",
                "test request",
            ),
        )


from dataclasses import replace

from research_store.config import StoreConfig
from research_store.container import build_evidence_service


@pytest.fixture(scope="session")
def prepared_database_for_evidence_packets(prepared_database_for_claims):
    # prepared_database_for_claims already upgrades to head which now includes 0029
    pass


@INTEGRATION_MARK
def test_evidence_packet_persistence_and_immutability(
    tmp_path, prepared_database_for_evidence_packets
):
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    svc = build_evidence_service(config)
    run_id = uuid4()
    ensure_run_exists(TEST_DSN, run_id)
    spec_id = uuid4()

    caps = ResourceCaps.from_mapping(
        {
            **DEFAULT_POLICY.profiles["standard"].to_dict(),
            "max_evidence_packet_tokens": 8000,
        }
    )

    packet1 = svc.build_evidence_packet(
        run_id=run_id,
        research_spec_id=spec_id,
        coverage_revision=1,
        candidates=[_make_candidate()],
        retrieval_events=[],
        effective_caps=caps,
    )

    # Persist first packet
    rev1 = svc.persist_packet(packet1)
    assert rev1 == 1

    # Persist second packet (simulate a new revision)
    packet2 = svc.build_evidence_packet(
        run_id=run_id,
        research_spec_id=spec_id,
        coverage_revision=2,
        candidates=[_make_candidate(), _make_candidate()],
        retrieval_events=[],
        effective_caps=caps,
    )
    rev2 = svc.persist_packet(packet2)
    assert rev2 == 2

    # Export them back
    exported1 = svc.export_packet(run_id, 1)
    assert exported1["packet_revision"] == 1
    assert exported1["coverage_revision"] == 1
    assert len(exported1["payload"]["passages"]) == 1

    exported2 = svc.export_packet(run_id, 2)
    assert exported2["packet_revision"] == 2
    assert exported2["coverage_revision"] == 2
    assert len(exported2["payload"]["passages"]) == 2

    # Export latest (should be 2)
    exported_latest = svc.export_packet(run_id)
    assert exported_latest["packet_revision"] == 2


@INTEGRATION_MARK
def test_evidence_packet_unique_constraint_violation(
    tmp_path, prepared_database_for_evidence_packets
):
    """Persisting a duplicate (run_id, packet_revision) raises a unique violation."""
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
    )
    svc = build_evidence_service(config)
    run_id = uuid4()
    ensure_run_exists(TEST_DSN, run_id)
    spec_id = uuid4()

    caps = ResourceCaps.from_mapping(
        {
            **DEFAULT_POLICY.profiles["standard"].to_dict(),
            "max_evidence_packet_tokens": 8000,
        }
    )

    packet = svc.build_evidence_packet(
        run_id=run_id,
        research_spec_id=spec_id,
        coverage_revision=1,
        candidates=[_make_candidate()],
        retrieval_events=[],
        effective_caps=caps,
    )

    # Persist first packet (revision 1)
    rev1 = svc.persist_packet(packet)
    assert rev1 == 1

    # Build a second packet with the same run_id but same coverage_revision
    # so it gets revision 1 again — this should violate the UNIQUE constraint.
    packet2 = svc.build_evidence_packet(
        run_id=run_id,
        research_spec_id=spec_id,
        coverage_revision=1,  # same coverage revision → same packet_revision
        candidates=[_make_candidate()],
        retrieval_events=[],
        effective_caps=caps,
    )
    with pytest.raises(Exception):  # noqa: B017, psycopg UniqueViolation
        svc.persist_packet(packet2)


@INTEGRATION_MARK
def test_export_packet_returns_none_for_missing_run():
    """export_packet returns None when no packet exists for the given run_id."""
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=Path("/tmp/evidence-test-blobs"),
    )
    svc = build_evidence_service(config)
    missing_run = uuid4()

    # Should return None for a run that has no packets
    assert svc.export_packet(missing_run) is None
    assert svc.export_packet(missing_run, revision=1) is None


def test_evidence_packet_referential_integrity():
    """EvidencePacket rejects unknown passage IDs in bindings and groups."""
    from research_domain.models import (
        ClaimEvidenceBinding,
        EvidenceClaim,
        EvidenceGroup,
        EvidencePacket,
        EvidencePassage,
        EvidenceRelationship,
        SemanticStatus,
    )

    run_id = uuid4()
    spec_id = uuid4()
    passage_id = uuid4()
    claim_id = uuid4()
    fake_passage_id = uuid4()
    fake_group_id = uuid4()

    # Valid claim referencing the passage
    claim = EvidenceClaim(
        claim_id=claim_id,
        statement="This is a test claim",
        semantic_status=SemanticStatus.UNASSESSED,
        uncertainty="low",
    )

    # Valid binding referencing the claim and passage
    binding = ClaimEvidenceBinding(
        binding_id=uuid4(),
        claim_id=claim_id,
        passage_ids=(passage_id,),
        relationship=EvidenceRelationship.SUPPORTS,
        confidence=0.9,
        uncertainty="low",
    )

    # Valid group referencing the passage
    group = EvidenceGroup(
        group_id=fake_group_id,
        passage_ids=(passage_id,),
        rationale="test",
        evaluated=True,
    )

    # Construct a valid packet first — should succeed
    valid_packet = EvidencePacket(
        schema_version=EvidencePacket.SCHEMA_VERSION,
        run_id=run_id,
        research_spec_id=spec_id,
        coverage_revision=1,
        claims=(claim,),
        passages=(
            EvidencePassage(
                passage_id=passage_id,
                candidate_id=uuid4(),
                snapshot_id=uuid4(),
                chunk_id=uuid4(),
                text="test text",
                source_url="https://example.com",
            ),
        ),
        omitted_passages=(),
        claim_evidence_bindings=(binding,),
        corroborating_groups=(group,),
        contradicting_groups=(),
        qualifying_groups=(),
        near_duplicate_groups=(),
        source_diversity_summary={"unique_sources": 1, "sources": ["https://example.com"]},
        freshness_summary={"most_recent": None, "oldest": None},
        limitations=(),
        unresolved_items=(),
        retrieval_provenance=(),
    )
    assert valid_packet is not None

    # Now construct an invalid packet with a binding referencing
    # a passage_id that does not exist in the packet.
    invalid_binding = ClaimEvidenceBinding(
        binding_id=uuid4(),
        claim_id=claim_id,
        passage_ids=(fake_passage_id,),
        relationship=EvidenceRelationship.SUPPORTS,
        confidence=0.9,
        uncertainty="low",
    )

    # __post_init__ should reject the unknown passage ID.
    with pytest.raises(ValueError, match="unknown passage IDs"):
        EvidencePacket(
            schema_version=EvidencePacket.SCHEMA_VERSION,
            run_id=run_id,
            research_spec_id=spec_id,
            coverage_revision=1,
            claims=(claim,),
            passages=(
                EvidencePassage(
                    passage_id=passage_id,
                    candidate_id=uuid4(),
                    snapshot_id=uuid4(),
                    chunk_id=uuid4(),
                    text="test text",
                    source_url="https://example.com",
                ),
            ),
            omitted_passages=(),
            claim_evidence_bindings=(invalid_binding,),
            corroborating_groups=(),
            contradicting_groups=(),
            qualifying_groups=(),
            near_duplicate_groups=(),
            source_diversity_summary={"unique_sources": 1, "sources": ["https://example.com"]},
            freshness_summary={"most_recent": None, "oldest": None},
            limitations=(),
            unresolved_items=(),
            retrieval_provenance=(),
        )
