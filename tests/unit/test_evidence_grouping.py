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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Corroboration tests
# ---------------------------------------------------------------------------


def test_single_support_binding_creates_corroborating_group(service):
    """A single supports binding produces one corroborating group."""
    claim = _make_claim()
    passage = _make_passage(source_url="https://independent.com/a")
    binding = _make_binding(claim_id=claim.claim_id, passage_ids=[passage.passage_id])

    packet = _make_packet(
        claims=[claim],
        passages=[passage],
        bindings=[binding],
    )

    result = service.group_evidence(packet)

    assert len(result["corroborating_groups"]) == 1
    group = result["corroborating_groups"][0]
    assert group.evaluated is True
    assert len(group.passage_ids) == 1
    assert group.passage_ids[0] == passage.passage_id
    assert "supports" in group.rationale.lower()
    assert "independent" in group.rationale.lower()


def test_multiple_independent_sources_creates_multiple_groups(service):
    """Each supports binding is a separate group; independent sources noted."""
    claim = _make_claim()
    p1 = _make_passage(source_url="https://source-a.com/article")
    p2 = _make_passage(source_url="https://source-b.com/article")
    b1 = _make_binding(claim_id=claim.claim_id, passage_ids=[p1.passage_id])
    b2 = _make_binding(
        claim_id=claim.claim_id,
        passage_ids=[p2.passage_id],
        relationship=EvidenceRelationship.SUPPORTS,
    )

    packet = _make_packet(
        claims=[claim],
        passages=[p1, p2],
        bindings=[b1, b2],
    )

    result = service.group_evidence(packet)

    assert len(result["corroborating_groups"]) == 2
    # Each group should reference exactly one passage
    for g in result["corroborating_groups"]:
        assert len(g.passage_ids) == 1
        assert g.evaluated is True


def test_repeated_reporting_same_source(service):
    """Same source URL in multiple bindings is noted as single-source."""
    claim = _make_claim()
    passage = _make_passage(source_url="https://same-source.com/article")
    b1 = _make_binding(claim_id=claim.claim_id, passage_ids=[passage.passage_id])
    b2 = _make_binding(
        claim_id=claim.claim_id,
        passage_ids=[passage.passage_id],
        confidence=0.7,
    )

    packet = _make_packet(
        claims=[claim],
        passages=[passage],
        bindings=[b1, b2],
    )

    result = service.group_evidence(packet)

    assert len(result["corroborating_groups"]) == 2
    # Both groups should reference the same passage
    for g in result["corroborating_groups"]:
        assert g.passage_ids[0] == passage.passage_id
    # Rationale should note single-source limitation
    all_rationales = [g.rationale for g in result["corroborating_groups"]]
    assert any("single-source" in r.lower() for r in all_rationales)


# ---------------------------------------------------------------------------
# Contradiction tests
# ---------------------------------------------------------------------------


def test_contradicting_evidence_preserved(service):
    """Contradicting bindings are preserved, not silently dropped."""
    claim = _make_claim()
    passage = _make_passage(source_url="https://contradiction.com/article")
    binding = _make_binding(
        claim_id=claim.claim_id,
        passage_ids=[passage.passage_id],
        relationship=EvidenceRelationship.CONTRADICTS,
        confidence=0.85,
    )

    packet = _make_packet(
        claims=[claim],
        passages=[passage],
        bindings=[binding],
    )

    result = service.group_evidence(packet)

    assert len(result["contradicting_groups"]) == 1
    group = result["contradicting_groups"][0]
    assert group.evaluated is True
    assert group.passage_ids[0] == passage.passage_id
    assert "contradicted" in group.rationale.lower()
    assert "preserved" in group.rationale.lower()


def test_mixed_support_and_contradict(service):
    """Both corroborating and contradicting groups coexist."""
    claim = _make_claim()
    support_passage = _make_passage(source_url="https://support.com/a")
    contradict_passage = _make_passage(source_url="https://contradict.com/b")
    support_binding = _make_binding(
        claim_id=claim.claim_id,
        passage_ids=[support_passage.passage_id],
    )
    contradict_binding = _make_binding(
        claim_id=claim.claim_id,
        passage_ids=[contradict_passage.passage_id],
        relationship=EvidenceRelationship.CONTRADICTS,
    )

    packet = _make_packet(
        claims=[claim],
        passages=[support_passage, contradict_passage],
        bindings=[support_binding, contradict_binding],
    )

    result = service.group_evidence(packet)

    assert len(result["corroborating_groups"]) == 1
    assert len(result["contradicting_groups"]) == 1
    # Contradicting group should reference the contradict passage
    assert (
        result["contradicting_groups"][0].passage_ids[0]
        == contradict_passage.passage_id
    )


# ---------------------------------------------------------------------------
# Qualification tests
# ---------------------------------------------------------------------------


def test_qualifying_group_created(service):
    """A qualifies binding produces a qualifying group."""
    claim = _make_claim()
    passage = _make_passage(source_url="https://qualification.com/a")
    binding = _make_binding(
        claim_id=claim.claim_id,
        passage_ids=[passage.passage_id],
        relationship=EvidenceRelationship.QUALIFIES,
        confidence=0.6,
    )

    packet = _make_packet(
        claims=[claim],
        passages=[passage],
        bindings=[binding],
    )

    result = service.group_evidence(packet)

    assert len(result["qualifying_groups"]) == 1
    group = result["qualifying_groups"][0]
    assert group.evaluated is True
    assert group.passage_ids[0] == passage.passage_id
    assert "qualified" in group.rationale.lower()
    assert "contextual" in group.rationale.lower()


# ---------------------------------------------------------------------------
# Context tests
# ---------------------------------------------------------------------------


def test_context_binding_is_discarded(service):
    """A context binding produces no group — context_groups is not returned."""
    claim = _make_claim()
    passage = _make_passage(source_url="https://context.com/a")
    binding = _make_binding(
        claim_id=claim.claim_id,
        passage_ids=[passage.passage_id],
        relationship=EvidenceRelationship.CONTEXT,
        confidence=0.5,
    )

    packet = _make_packet(
        claims=[claim],
        passages=[passage],
        bindings=[binding],
    )

    result = service.group_evidence(packet)

    # context_groups is no longer returned; only the three top-level keys.
    assert "context_groups" not in result
    assert len(result["corroborating_groups"]) == 0
    assert len(result["contradicting_groups"]) == 0
    assert len(result["qualifying_groups"]) == 0


# ---------------------------------------------------------------------------
# Evaluated absence vs unevaluated state
# ---------------------------------------------------------------------------


def test_unsupported_claim_unevaluated(service):
    """Unsupported claim produces an unevaluated corroborating group."""
    claim = _make_claim(
        semantic_status=SemanticStatus.UNSUPPORTED,
        statement="Claim with no evidence",
    )

    packet = _make_packet(claims=[claim], passages=(), bindings=())

    result = service.group_evidence(packet)

    # Should have an unevaluated group for the unsupported claim
    assert len(result["corroborating_groups"]) >= 1
    unevaluated = [g for g in result["corroborating_groups"] if not g.evaluated]
    assert len(unevaluated) >= 1
    assert "unsupported" in unevaluated[0].rationale.lower()
    assert unevaluated[0].passage_ids == ()


def test_unassessed_claim_unevaluated(service):
    """Unassessed claim produces an unevaluated corroborating group."""
    claim = _make_claim(
        semantic_status=SemanticStatus.UNASSESSED,
        statement="Claim awaiting evaluation",
    )

    packet = _make_packet(claims=[claim], passages=(), bindings=())

    result = service.group_evidence(packet)

    unevaluated = [g for g in result["corroborating_groups"] if not g.evaluated]
    assert len(unevaluated) >= 1
    assert "unassessed" in unevaluated[0].rationale.lower()


def test_uncertain_claim_unevaluated(service):
    """Uncertain claim produces an unevaluated corroborating group."""
    claim = _make_claim(
        semantic_status=SemanticStatus.UNCERTAIN,
        statement="Claim with uncertain status",
    )

    packet = _make_packet(claims=[claim], passages=(), bindings=())

    result = service.group_evidence(packet)

    unevaluated = [g for g in result["corroborating_groups"] if not g.evaluated]
    assert len(unevaluated) >= 1
    assert "uncertain" in unevaluated[0].rationale.lower()


def test_empty_packet_returns_empty_groups(service):
    """An empty packet (no claims, no passages, no bindings) returns empty groups."""
    packet = _make_packet()

    result = service.group_evidence(packet)

    assert len(result["corroborating_groups"]) == 0
    assert len(result["contradicting_groups"]) == 0
    assert len(result["qualifying_groups"]) == 0
    # context_groups is not returned.
    assert "context_groups" not in result


def test_bindings_no_claims_returns_empty_groups(service):
    """Bindings with unknown claim IDs are rejected by EvidencePacket constructor.

    The EvidencePacket.__post_init__ validates that all binding claim_ids
    must exist in the claims list.  This test verifies that validation.
    """
    passage = _make_passage()
    claim_id = uuid4()
    binding = _make_binding(
        claim_id=claim_id,
        passage_ids=[passage.passage_id],
    )

    # No claims in the packet, but binding references a claim_id.
    # EvidencePacket.__post_init__ should reject this.
    with pytest.raises(ValueError, match="unknown evidence claim IDs"):
        _make_packet(claims=[], passages=[passage], bindings=[binding])


# ---------------------------------------------------------------------------
# build_packet_with_groups integration
# ---------------------------------------------------------------------------


def test_build_packet_with_groups_populates_fields(service):
    """build_packet_with_groups returns a new packet with groups populated."""
    claim = _make_claim()
    passage = _make_passage(source_url="https://example.com/a")
    binding = _make_binding(claim_id=claim.claim_id, passage_ids=[passage.passage_id])

    packet = _make_packet(
        claims=[claim],
        passages=[passage],
        bindings=[binding],
    )

    new_packet = service.build_packet_with_groups(packet)

    assert len(new_packet.corroborating_groups) == 1
    assert len(new_packet.contradicting_groups) == 0
    assert len(new_packet.qualifying_groups) == 0
    # Original fields preserved
    assert new_packet.passages == packet.passages
    assert new_packet.claim_evidence_bindings == packet.claim_evidence_bindings
    assert new_packet.near_duplicate_groups == packet.near_duplicate_groups


def test_build_packet_with_groups_preserves_near_duplicate_groups(service):
    """Near-duplicate groups are preserved through grouping."""
    dup_group = EvidenceGroup(
        group_id=uuid4(),
        passage_ids=(),
        rationale="test duplicate",
        evaluated=False,
    )
    claim = _make_claim()
    passage = _make_passage()
    binding = _make_binding(claim_id=claim.claim_id, passage_ids=[passage.passage_id])

    packet = _make_packet(
        claims=[claim],
        passages=[passage],
        bindings=[binding],
        near_duplicate_groups=(dup_group,),
    )

    new_packet = service.build_packet_with_groups(packet)

    assert len(new_packet.near_duplicate_groups) == 1
    assert new_packet.near_duplicate_groups[0].rationale == "test duplicate"


# ---------------------------------------------------------------------------
# Evaluated absence through build_packet_with_groups
# ---------------------------------------------------------------------------


def test_build_packet_with_groups_unsupported_claim(service):
    """build_packet_with_groups succeeds with unsupported claim (no bindings)."""
    claim = _make_claim(
        semantic_status=SemanticStatus.UNSUPPORTED,
        statement="Unsupported claim",
    )

    packet = _make_packet(claims=[claim], passages=(), bindings=())

    new_packet = service.build_packet_with_groups(packet)

    # Should have one unevaluated corroborating group
    assert len(new_packet.corroborating_groups) == 1
    group = new_packet.corroborating_groups[0]
    assert group.evaluated is False
    assert group.passage_ids == ()
    assert "unsupported" in group.rationale.lower()
    # Other fields preserved
    assert new_packet.passages == ()
    assert new_packet.claim_evidence_bindings == ()


def test_build_packet_with_groups_unassessed_claim(service):
    """build_packet_with_groups succeeds with unassessed claim (no bindings)."""
    claim = _make_claim(
        semantic_status=SemanticStatus.UNASSESSED,
        statement="Unassessed claim",
    )

    packet = _make_packet(claims=[claim], passages=(), bindings=())

    new_packet = service.build_packet_with_groups(packet)

    assert len(new_packet.corroborating_groups) == 1
    group = new_packet.corroborating_groups[0]
    assert group.evaluated is False
    assert group.passage_ids == ()
    assert "unassessed" in group.rationale.lower()


def test_build_packet_with_groups_uncertain_claim(service):
    """build_packet_with_groups succeeds with uncertain claim (no bindings)."""
    claim = _make_claim(
        semantic_status=SemanticStatus.UNCERTAIN,
        statement="Uncertain claim",
    )

    packet = _make_packet(claims=[claim], passages=(), bindings=())

    new_packet = service.build_packet_with_groups(packet)

    assert len(new_packet.corroborating_groups) == 1
    group = new_packet.corroborating_groups[0]
    assert group.evaluated is False
    assert group.passage_ids == ()
    assert "uncertain" in group.rationale.lower()


def test_build_packet_with_groups_supported_no_bindings(service):
    """build_packet_with_groups succeeds with supported claim but no bindings.

    This tests the "evaluated absence" path: the model said supported but
    produced no bindings.  The group must have evaluated=False because
    EvidenceGroup.__post_init__ rejects evaluated=True with empty passage_ids.
    """
    claim = _make_claim(
        semantic_status=SemanticStatus.SUPPORTED,
        statement="Supported claim with no bindings",
    )

    packet = _make_packet(claims=[claim], passages=(), bindings=())

    new_packet = service.build_packet_with_groups(packet)

    assert len(new_packet.corroborating_groups) == 1
    group = new_packet.corroborating_groups[0]
    assert group.evaluated is False
    assert group.passage_ids == ()
    assert "evaluated absence" in group.rationale.lower()


def test_build_packet_with_groups_contradicted_no_bindings(service):
    """build_packet_with_groups succeeds with contradicted claim but no bindings."""
    claim = _make_claim(
        semantic_status=SemanticStatus.CONTRADICTED,
        statement="Contradicted claim with no bindings",
    )

    packet = _make_packet(claims=[claim], passages=(), bindings=())

    new_packet = service.build_packet_with_groups(packet)

    assert len(new_packet.contradicting_groups) == 1
    group = new_packet.contradicting_groups[0]
    assert group.evaluated is False
    assert group.passage_ids == ()
    assert "evaluated absence" in group.rationale.lower()


def test_build_packet_with_groups_qualified_no_bindings(service):
    """build_packet_with_groups succeeds with qualified claim but no bindings."""
    claim = _make_claim(
        semantic_status=SemanticStatus.QUALIFIED,
        statement="Qualified claim with no bindings",
    )

    packet = _make_packet(claims=[claim], passages=(), bindings=())

    new_packet = service.build_packet_with_groups(packet)

    assert len(new_packet.qualifying_groups) == 1
    group = new_packet.qualifying_groups[0]
    assert group.evaluated is False
    assert group.passage_ids == ()
    assert "evaluated absence" in group.rationale.lower()


# ---------------------------------------------------------------------------
# Passage provenance
# ---------------------------------------------------------------------------


def test_exact_passage_ids_retained(service):
    """Group passage_ids exactly match the binding passage_ids."""
    claim = _make_claim()
    p1 = _make_passage(passage_id=UUID("00000000-0000-0000-0000-000000000001"))
    p2 = _make_passage(passage_id=UUID("00000000-0000-0000-0000-000000000002"))
    binding = _make_binding(
        claim_id=claim.claim_id,
        passage_ids=[p1.passage_id, p2.passage_id],
    )

    packet = _make_packet(
        claims=[claim],
        passages=[p1, p2],
        bindings=[binding],
    )

    result = service.group_evidence(packet)

    group = result["corroborating_groups"][0]
    assert set(group.passage_ids) == {p1.passage_id, p2.passage_id}


# ---------------------------------------------------------------------------
# Multiple claims
# ---------------------------------------------------------------------------


def test_multiple_claims_each_get_groups(service):
    """Each claim with bindings gets its own groups."""
    claim1 = _make_claim(claim_id=UUID("00000000-0000-0000-0000-000000000010"))
    claim2 = _make_claim(claim_id=UUID("00000000-0000-0000-0000-000000000020"))
    p1 = _make_passage(source_url="https://source1.com/a")
    p2 = _make_passage(source_url="https://source2.com/b")
    b1 = _make_binding(
        claim_id=claim1.claim_id,
        passage_ids=[p1.passage_id],
    )
    b2 = _make_binding(
        claim_id=claim2.claim_id,
        passage_ids=[p2.passage_id],
        relationship=EvidenceRelationship.CONTRADICTS,
    )

    packet = _make_packet(
        claims=[claim1, claim2],
        passages=[p1, p2],
        bindings=[b1, b2],
    )

    result = service.group_evidence(packet)

    assert len(result["corroborating_groups"]) == 1
    assert len(result["contradicting_groups"]) == 1


# ---------------------------------------------------------------------------
# EvidenceService.group_evidence integration (unit test with mock)
# ---------------------------------------------------------------------------


def test_evidence_service_group_evidence_raises_for_missing_packet():
    """EvidenceService.group_evidence raises ValueError for missing packet."""
    from unittest.mock import MagicMock

    from firecrawl_skill.research_store.budget_policy import DEFAULT_POLICY

    svc = __import__(
        "firecrawl_skill.research_store.assessment.evidence",
        fromlist=["EvidenceService"],
    ).EvidenceService(
        uow_factory=lambda: None,
        budget_policy=DEFAULT_POLICY,
    )

    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=False)
    mock_uow.get_evidence_packet = MagicMock(return_value=None)

    svc.uow_factory = lambda: mock_uow

    run_id = uuid4()
    with pytest.raises(ValueError, match="not found"):
        svc.group_evidence(run_id, revision=1)


def test_evidence_service_group_evidence_happy_path():
    """EvidenceService.group_evidence persists a new revision on success."""
    from unittest.mock import MagicMock

    from firecrawl_skill.research_store.budget_policy import DEFAULT_POLICY

    svc = __import__(
        "firecrawl_skill.research_store.assessment.evidence",
        fromlist=["EvidenceService"],
    ).EvidenceService(
        uow_factory=lambda: None,
        budget_policy=DEFAULT_POLICY,
    )

    run_id = uuid4()
    spec_id = uuid4()

    # Build a minimal packet dict with a claim and a binding.
    claim_id = uuid4()
    passage_id = uuid4()
    binding_id = uuid4()

    packet_dict = {
        "schema_version": "evidence-packet-v1",
        "run_id": str(run_id),
        "research_spec_id": str(spec_id),
        "coverage_revision": 1,
        "claims": [
            {
                "claim_id": str(claim_id),
                "statement": "Test claim",
                "semantic_status": "supported",
                "uncertainty": "low",
            },
        ],
        "passages": [
            {
                "passage_id": str(passage_id),
                "candidate_id": str(uuid4()),
                "snapshot_id": str(uuid4()),
                "chunk_id": str(uuid4()),
                "text": "test passage",
                "source_url": "https://example.com/source",
            },
        ],
        "omitted_passages": [],
        "claim_evidence_bindings": [
            {
                "binding_id": str(binding_id),
                "claim_id": str(claim_id),
                "passage_ids": [str(passage_id)],
                "relationship": "supports",
                "confidence": 0.9,
                "uncertainty": "low",
                "model": "test-model",
                "prompt_version": "v1",
                "schema_version": 1,
                "input_packet_revision": 1,
            },
        ],
        "corroborating_groups": [],
        "contradicting_groups": [],
        "qualifying_groups": [],
        "near_duplicate_groups": [],
        "source_diversity_summary": {"unique_sources": 1, "sources": []},
        "freshness_summary": {"most_recent": None, "oldest": None},
        "limitations": [],
        "unresolved_items": [],
        "independence_assessments": [],
        "retrieval_provenance": [],
    }

    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=False)

    # Mock the record returned by get_evidence_packet.
    record = MagicMock()
    record.to_dict = MagicMock(return_value=packet_dict)
    record.packet_revision = 1
    mock_uow.get_evidence_packet = MagicMock(return_value=record)

    svc.uow_factory = lambda: mock_uow

    new_rev = svc.group_evidence(run_id, revision=1)

    # Should have created a new revision (1 → 2).
    assert new_rev == 2

    # Should have called persist_evidence_packet with the new revision.
    mock_uow.persist_evidence_packet.assert_called_once()
    call_args = mock_uow.persist_evidence_packet.call_args
    assert call_args[0][3] == 2  # packet_revision argument


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_group_evidence_ignores_omitted_passages_for_source_urls(service):
    """Source URLs from omitted passages are also considered."""
    claim = _make_claim()
    omitted = _make_passage(source_url="https://omitted.com/article")
    binding = _make_binding(
        claim_id=claim.claim_id,
        passage_ids=[omitted.passage_id],
    )

    packet = _make_packet(
        claims=[claim],
        passages=[],
        omitted_passages=[omitted],
        bindings=[binding],
    )

    result = service.group_evidence(packet)

    assert len(result["corroborating_groups"]) == 1
    group = result["corroborating_groups"][0]
    assert omitted.passage_id in group.passage_ids


def test_group_ids_are_unique(service):
    """Each group gets a unique ID via uuid4()."""
    claim = _make_claim()
    p1 = _make_passage(source_url="https://a.com")
    p2 = _make_passage(source_url="https://b.com")
    b1 = _make_binding(claim_id=claim.claim_id, passage_ids=[p1.passage_id])
    b2 = _make_binding(
        claim_id=claim.claim_id,
        passage_ids=[p2.passage_id],
    )

    packet = _make_packet(
        claims=[claim],
        passages=[p1, p2],
        bindings=[b1, b2],
    )

    result = service.group_evidence(packet)

    all_group_ids = []
    for groups in result.values():
        all_group_ids.extend(g.group_id for g in groups)
    assert len(all_group_ids) == len(set(all_group_ids))


def test_group_rationale_includes_claim_statement(service):
    """Group rationale includes the claim statement for traceability."""
    statement = "This is a specific claim statement"
    claim = _make_claim(statement=statement)
    passage = _make_passage()
    binding = _make_binding(claim_id=claim.claim_id, passage_ids=[passage.passage_id])

    packet = _make_packet(
        claims=[claim],
        passages=[passage],
        bindings=[binding],
    )

    result = service.group_evidence(packet)

    group = result["corroborating_groups"][0]
    assert statement in group.rationale
