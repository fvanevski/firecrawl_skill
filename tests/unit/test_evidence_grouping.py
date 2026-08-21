"""Tests for EvidenceGroupingService (issue #56).

Covers:
- Corroboration grouping from supports bindings.
- Contradiction grouping from contradicts bindings.
- Qualification grouping from qualifies bindings.
- Context bindings are silently discarded (not returned).
- Evaluated absence vs unevaluated state.
- Source independence: independent corroboration vs repeated reporting.
- Contradictory evidence preserved (not silently dropped).
- Empty packet handling.
- Multiple bindings per claim.
- Passage provenance retained exactly.
"""

from __future__ import annotations

import sys
from uuid import UUID, uuid4

import pytest

SCRIPTS = __import__("pathlib").Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from firecrawl_skill.research_domain.models import (
    EvidenceClaim,
    EvidenceGroup,
    EvidencePacket,
    EvidencePassage,
    EvidenceRelationship,
    SemanticStatus,
)
from firecrawl_skill.research_store.assessment.grouping import EvidenceGroupingService


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


def _make_claim(
    claim_id=None,
    statement="Test claim statement",
    semantic_status=SemanticStatus.UNASSESSED,
    uncertainty="low",
):
    return EvidenceClaim(
        claim_id=claim_id or uuid4(),
        statement=statement,
        semantic_status=semantic_status,
        uncertainty=uncertainty,
    )


def _make_binding(
    binding_id=None,
    claim_id=None,
    passage_ids=None,
    relationship=EvidenceRelationship.SUPPORTS,
    confidence=0.9,
    uncertainty="low",
):
    return __import__(
        "firecrawl_skill.research_domain.models", fromlist=["ClaimEvidenceBinding"]
    ).ClaimEvidenceBinding(
        binding_id=binding_id or uuid4(),
        claim_id=claim_id or uuid4(),
        passage_ids=tuple(passage_ids) if passage_ids else (),
        relationship=relationship,
        confidence=confidence,
        uncertainty=uncertainty,
        model="test-model",
        prompt_version="v1",
        schema_version=1,
        input_packet_revision=1,
    )


def _make_packet(
    claims=None,
    passages=None,
    omitted_passages=None,
    bindings=None,
    run_id=None,
    research_spec_id=None,
    coverage_revision=1,
    near_duplicate_groups=(),
    independence_assessments=(),
    retrieval_provenance=(),
):
    return EvidencePacket(
        schema_version=EvidencePacket.SCHEMA_VERSION,
        run_id=run_id or uuid4(),
        research_spec_id=research_spec_id or uuid4(),
        coverage_revision=coverage_revision,
        claims=tuple(claims) if claims else (),
        passages=tuple(passages) if passages else (),
        omitted_passages=tuple(omitted_passages) if omitted_passages else (),
        claim_evidence_bindings=tuple(bindings) if bindings else (),
        corroborating_groups=(),
        contradicting_groups=(),
        qualifying_groups=(),
        near_duplicate_groups=near_duplicate_groups,
        source_diversity_summary={"unique_sources": 0, "sources": []},
        freshness_summary={"most_recent": None, "oldest": None},
        limitations=(),
        unresolved_items=(),
        independence_assessments=independence_assessments,
        retrieval_provenance=retrieval_provenance,
    )


@pytest.fixture
def service():
    return EvidenceGroupingService()


def test_single_support_binding_creates_corroborating_group(service):
    claim = _make_claim()
    passage = _make_passage(source_url="https://independent.com/a")
    binding = _make_binding(claim_id=claim.claim_id, passage_ids=[passage.passage_id])
    result = service.group_evidence(
        _make_packet(claims=[claim], passages=[passage], bindings=[binding])
    )
    assert len(result["corroborating_groups"]) == 1
    group = result["corroborating_groups"][0]
    assert group.evaluated is True
    assert group.passage_ids == (passage.passage_id,)
    assert "supports" in group.rationale.lower()
    assert "independent" in group.rationale.lower()


def test_multiple_independent_sources_creates_multiple_groups(service):
    claim = _make_claim()
    p1 = _make_passage(source_url="https://source-a.com/article")
    p2 = _make_passage(source_url="https://source-b.com/article")
    b1 = _make_binding(claim_id=claim.claim_id, passage_ids=[p1.passage_id])
    b2 = _make_binding(claim_id=claim.claim_id, passage_ids=[p2.passage_id])
    result = service.group_evidence(
        _make_packet(claims=[claim], passages=[p1, p2], bindings=[b1, b2])
    )
    assert len(result["corroborating_groups"]) == 2
    assert all(len(group.passage_ids) == 1 for group in result["corroborating_groups"])


def test_repeated_reporting_same_source(service):
    claim = _make_claim()
    passage = _make_passage(source_url="https://same-source.com/article")
    b1 = _make_binding(claim_id=claim.claim_id, passage_ids=[passage.passage_id])
    b2 = _make_binding(
        claim_id=claim.claim_id, passage_ids=[passage.passage_id], confidence=0.7
    )
    result = service.group_evidence(
        _make_packet(claims=[claim], passages=[passage], bindings=[b1, b2])
    )
    assert len(result["corroborating_groups"]) == 2
    assert all(
        group.passage_ids[0] == passage.passage_id
        for group in result["corroborating_groups"]
    )
    assert any(
        "single-source" in group.rationale.lower()
        for group in result["corroborating_groups"]
    )


def test_contradicting_evidence_preserved(service):
    claim = _make_claim()
    passage = _make_passage(source_url="https://contradiction.com/article")
    binding = _make_binding(
        claim_id=claim.claim_id,
        passage_ids=[passage.passage_id],
        relationship=EvidenceRelationship.CONTRADICTS,
        confidence=0.85,
    )
    result = service.group_evidence(
        _make_packet(claims=[claim], passages=[passage], bindings=[binding])
    )
    group = result["contradicting_groups"][0]
    assert group.evaluated is True
    assert group.passage_ids[0] == passage.passage_id
    assert "contradicted" in group.rationale.lower()
    assert "preserved" in group.rationale.lower()


def test_mixed_support_and_contradict(service):
    claim = _make_claim()
    support = _make_passage(source_url="https://support.com/a")
    contradict = _make_passage(source_url="https://contradict.com/b")
    bindings = [
        _make_binding(claim_id=claim.claim_id, passage_ids=[support.passage_id]),
        _make_binding(
            claim_id=claim.claim_id,
            passage_ids=[contradict.passage_id],
            relationship=EvidenceRelationship.CONTRADICTS,
        ),
    ]
    result = service.group_evidence(
        _make_packet(
            claims=[claim], passages=[support, contradict], bindings=bindings
        )
    )
    assert len(result["corroborating_groups"]) == 1
    assert len(result["contradicting_groups"]) == 1


def test_qualifying_group_created(service):
    claim = _make_claim()
    passage = _make_passage(source_url="https://qualification.com/a")
    binding = _make_binding(
        claim_id=claim.claim_id,
        passage_ids=[passage.passage_id],
        relationship=EvidenceRelationship.QUALIFIES,
        confidence=0.6,
    )
    result = service.group_evidence(
        _make_packet(claims=[claim], passages=[passage], bindings=[binding])
    )
    group = result["qualifying_groups"][0]
    assert group.evaluated is True
    assert "qualified" in group.rationale.lower()
    assert "contextual" in group.rationale.lower()


def test_context_binding_is_discarded(service):
    claim = _make_claim()
    passage = _make_passage(source_url="https://context.com/a")
    binding = _make_binding(
        claim_id=claim.claim_id,
        passage_ids=[passage.passage_id],
        relationship=EvidenceRelationship.CONTEXT,
    )
    result = service.group_evidence(
        _make_packet(claims=[claim], passages=[passage], bindings=[binding])
    )
    assert "context_groups" not in result
    assert not result["corroborating_groups"]
    assert not result["contradicting_groups"]
    assert not result["qualifying_groups"]


@pytest.mark.parametrize(
    ("status", "needle"),
    [
        (SemanticStatus.UNSUPPORTED, "unsupported"),
        (SemanticStatus.UNASSESSED, "unassessed"),
        (SemanticStatus.UNCERTAIN, "uncertain"),
    ],
)
def test_claim_without_binding_is_unevaluated(service, status, needle):
    packet = _make_packet(claims=[_make_claim(semantic_status=status)])
    result = service.group_evidence(packet)
    groups = [group for group in result["corroborating_groups"] if not group.evaluated]
    assert groups
    assert needle in groups[0].rationale.lower()


def test_empty_packet_returns_empty_groups(service):
    result = service.group_evidence(_make_packet())
    assert all(not groups for groups in result.values())
    assert "context_groups" not in result


def test_bindings_no_claims_returns_empty_groups(service):
    passage = _make_passage()
    binding = _make_binding(claim_id=uuid4(), passage_ids=[passage.passage_id])
    with pytest.raises(ValueError, match="unknown evidence claim IDs"):
        _make_packet(claims=[], passages=[passage], bindings=[binding])


def test_build_packet_with_groups_populates_fields(service):
    claim = _make_claim()
    passage = _make_passage(source_url="https://example.com/a")
    binding = _make_binding(claim_id=claim.claim_id, passage_ids=[passage.passage_id])
    packet = _make_packet(claims=[claim], passages=[passage], bindings=[binding])
    grouped = service.build_packet_with_groups(packet)
    assert len(grouped.corroborating_groups) == 1
    assert grouped.passages == packet.passages
    assert grouped.claim_evidence_bindings == packet.claim_evidence_bindings


def test_build_packet_with_groups_preserves_near_duplicate_groups(service):
    duplicate = EvidenceGroup(
        group_id=uuid4(), passage_ids=(), rationale="test duplicate", evaluated=False
    )
    claim = _make_claim()
    passage = _make_passage()
    binding = _make_binding(claim_id=claim.claim_id, passage_ids=[passage.passage_id])
    grouped = service.build_packet_with_groups(
        _make_packet(
            claims=[claim],
            passages=[passage],
            bindings=[binding],
            near_duplicate_groups=(duplicate,),
        )
    )
    assert grouped.near_duplicate_groups == (duplicate,)


@pytest.mark.parametrize(
    ("status", "group_key", "needle"),
    [
        (SemanticStatus.UNSUPPORTED, "corroborating_groups", "unsupported"),
        (SemanticStatus.UNASSESSED, "corroborating_groups", "unassessed"),
        (SemanticStatus.UNCERTAIN, "corroborating_groups", "uncertain"),
        (SemanticStatus.SUPPORTED, "corroborating_groups", "evaluated absence"),
        (SemanticStatus.CONTRADICTED, "contradicting_groups", "evaluated absence"),
        (SemanticStatus.QUALIFIED, "qualifying_groups", "evaluated absence"),
    ],
)
def test_build_packet_with_groups_no_binding_status(service, status, group_key, needle):
    grouped = service.build_packet_with_groups(
        _make_packet(claims=[_make_claim(semantic_status=status)])
    )
    groups = getattr(grouped, group_key)
    assert len(groups) == 1
    assert groups[0].evaluated is False
    assert groups[0].passage_ids == ()
    assert needle in groups[0].rationale.lower()


def test_exact_passage_ids_retained(service):
    claim = _make_claim()
    p1 = _make_passage(passage_id=UUID("00000000-0000-0000-0000-000000000001"))
    p2 = _make_passage(passage_id=UUID("00000000-0000-0000-0000-000000000002"))
    binding = _make_binding(
        claim_id=claim.claim_id, passage_ids=[p1.passage_id, p2.passage_id]
    )
    result = service.group_evidence(
        _make_packet(claims=[claim], passages=[p1, p2], bindings=[binding])
    )
    assert set(result["corroborating_groups"][0].passage_ids) == {
        p1.passage_id,
        p2.passage_id,
    }


def test_multiple_claims_each_get_groups(service):
    claim1 = _make_claim()
    claim2 = _make_claim()
    p1 = _make_passage(source_url="https://source1.com/a")
    p2 = _make_passage(source_url="https://source2.com/b")
    bindings = [
        _make_binding(claim_id=claim1.claim_id, passage_ids=[p1.passage_id]),
        _make_binding(
            claim_id=claim2.claim_id,
            passage_ids=[p2.passage_id],
            relationship=EvidenceRelationship.CONTRADICTS,
        ),
    ]
    result = service.group_evidence(
        _make_packet(claims=[claim1, claim2], passages=[p1, p2], bindings=bindings)
    )
    assert len(result["corroborating_groups"]) == 1
    assert len(result["contradicting_groups"]) == 1


def test_evidence_service_group_evidence_raises_for_missing_packet():
    from unittest.mock import MagicMock

    from firecrawl_skill.research_store.assessment.evidence import EvidenceService
    from firecrawl_skill.research_store.budget_policy import DEFAULT_POLICY

    svc = EvidenceService(uow_factory=lambda: None, budget_policy=DEFAULT_POLICY)
    mock_uow = MagicMock()
    mock_uow.__enter__.return_value = mock_uow
    mock_uow.__exit__.return_value = False
    mock_uow.evidence_packets.get_evidence_packet.return_value = None
    svc.uow_factory = lambda: mock_uow
    with pytest.raises(ValueError, match="not found"):
        svc.group_evidence(uuid4(), revision=1)


def test_evidence_service_group_evidence_happy_path():
    from unittest.mock import MagicMock

    from firecrawl_skill.research_store.assessment.evidence import EvidenceService
    from firecrawl_skill.research_store.budget_policy import DEFAULT_POLICY

    svc = EvidenceService(uow_factory=lambda: None, budget_policy=DEFAULT_POLICY)
    run_id = uuid4()
    spec_id = uuid4()
    claim_id = uuid4()
    passage_id = uuid4()
    packet_dict = {
        "schema_version": "evidence-packet-v1",
        "run_id": str(run_id),
        "research_spec_id": str(spec_id),
        "coverage_revision": 1,
        "claims": [{"claim_id": str(claim_id), "statement": "Test claim", "semantic_status": "supported", "uncertainty": "low"}],
        "passages": [{"passage_id": str(passage_id), "candidate_id": str(uuid4()), "snapshot_id": str(uuid4()), "chunk_id": str(uuid4()), "text": "test passage", "source_url": "https://example.com/source"}],
        "omitted_passages": [],
        "claim_evidence_bindings": [{"binding_id": str(uuid4()), "claim_id": str(claim_id), "passage_ids": [str(passage_id)], "relationship": "supports", "confidence": 0.9, "uncertainty": "low", "model": "test-model", "prompt_version": "v1", "schema_version": 1, "input_packet_revision": 1}],
        "corroborating_groups": [], "contradicting_groups": [], "qualifying_groups": [], "near_duplicate_groups": [],
        "source_diversity_summary": {"unique_sources": 1, "sources": []},
        "freshness_summary": {"most_recent": None, "oldest": None},
        "limitations": [], "unresolved_items": [], "independence_assessments": [], "retrieval_provenance": [],
    }
    mock_uow = MagicMock()
    mock_uow.__enter__.return_value = mock_uow
    mock_uow.__exit__.return_value = False
    record = MagicMock()
    record.to_dict.return_value = packet_dict
    record.packet_revision = 1
    mock_uow.evidence_packets.get_evidence_packet.return_value = record
    svc.uow_factory = lambda: mock_uow
    assert svc.group_evidence(run_id, revision=1) == 2
    mock_uow.evidence_packets.persist_evidence_packet.assert_called_once()
    assert mock_uow.evidence_packets.persist_evidence_packet.call_args[0][3] == 2


def test_group_evidence_ignores_omitted_passages_for_source_urls(service):
    claim = _make_claim()
    omitted = _make_passage(source_url="https://omitted.com/article")
    binding = _make_binding(claim_id=claim.claim_id, passage_ids=[omitted.passage_id])
    result = service.group_evidence(
        _make_packet(claims=[claim], omitted_passages=[omitted], bindings=[binding])
    )
    assert omitted.passage_id in result["corroborating_groups"][0].passage_ids


def test_group_ids_are_unique(service):
    claim = _make_claim()
    p1 = _make_passage(source_url="https://a.com")
    p2 = _make_passage(source_url="https://b.com")
    result = service.group_evidence(
        _make_packet(
            claims=[claim],
            passages=[p1, p2],
            bindings=[
                _make_binding(claim_id=claim.claim_id, passage_ids=[p1.passage_id]),
                _make_binding(claim_id=claim.claim_id, passage_ids=[p2.passage_id]),
            ],
        )
    )
    group_ids = [group.group_id for groups in result.values() for group in groups]
    assert len(group_ids) == len(set(group_ids))


def test_group_rationale_includes_claim_statement(service):
    statement = "This is a specific claim statement"
    claim = _make_claim(statement=statement)
    passage = _make_passage()
    binding = _make_binding(claim_id=claim.claim_id, passage_ids=[passage.passage_id])
    result = service.group_evidence(
        _make_packet(claims=[claim], passages=[passage], bindings=[binding])
    )
    assert statement in result["corroborating_groups"][0].rationale
