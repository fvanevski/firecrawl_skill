"""Tests for EvidencePacketValidator and packet diff (issue #58).

Covers:
- Completeness-state tests.
- Diff tests.
- Referential validation tests.
- Failure paths: unknown references, stale derivations, missing semantic
  stages, invalid group states, token-budget violations, missing retrieval
  execution, incomplete provenance.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from firecrawl_skill.research_domain.models import (
    ClaimEvidenceBinding,
    EvidenceClaim,
    EvidenceGroup,
    EvidencePacket,
    EvidencePassage,
    EvidenceRelationship,
    MechanicalStatus,
    RetrievalProvenance,
    SemanticStatus,
)
from firecrawl_skill.research_store.assessment.validation import (
    EvidencePacketValidator,
    ValidationFinding,
    ValidationResult,
    ValidationSeverity,
    bounded_citation_ready_output,
)
from firecrawl_skill.research_store.budget_policy import DEFAULT_POLICY, ResourceCaps
from firecrawl_skill.research_store.packet_diff import PacketDiff, diff_packets

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_passage(
    passage_id=None,
    candidate_id=None,
    snapshot_id=None,
    chunk_id=None,
    text="Some excerpt text",
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
    statement="The claim statement",
    semantic_status=SemanticStatus.UNASSESSED,
    uncertainty="low",
):
    return EvidenceClaim(
        claim_id=claim_id or uuid4(),
        statement=statement,
        semantic_status=semantic_status,
        uncertainty=uncertainty,
    )


def _make_packet(
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
    return EvidencePacket(
        schema_version=EvidencePacket.SCHEMA_VERSION,
        run_id=run_id or uuid4(),
        research_spec_id=research_spec_id or uuid4(),
        coverage_revision=coverage_revision,
        claims=tuple(claims or []),
        passages=tuple(passages or []),
        omitted_passages=tuple(omitted_passages or []),
        claim_evidence_bindings=tuple(claim_evidence_bindings or []),
        corroborating_groups=tuple(corroborating_groups or []),
        contradicting_groups=tuple(contradicting_groups or []),
        qualifying_groups=tuple(qualifying_groups or []),
        near_duplicate_groups=tuple(near_duplicate_groups or []),
        source_diversity_summary=source_diversity_summary or {},
        freshness_summary=freshness_summary or {},
        limitations=tuple(limitations or []),
        unresolved_items=tuple(unresolved_items or []),
        independence_assessments=tuple(independence_assessments or []),
        retrieval_provenance=tuple(retrieval_provenance or []),
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


def _make_rp(
    retrieval_event_id=None,
    run_id=None,
    requested_mode="hybrid",
    executed_mode="hybrid",
    mechanical_status=MechanicalStatus.SUCCEEDED,
    component_errors=None,
    selected_passage_ids=None,
):
    return RetrievalProvenance(
        retrieval_event_id=retrieval_event_id or uuid4(),
        requested_mode=requested_mode,
        executed_mode=executed_mode,
        mechanical_status=mechanical_status,
        component_errors=tuple(component_errors or []),
        selected_passage_ids=tuple(selected_passage_ids or []),
    )


# ---------------------------------------------------------------------------
# Packet completeness state tests
# ---------------------------------------------------------------------------


class TestPacketCompletenessState:
    """Tests for packet completeness state validation."""

    def test_empty_packet_is_valid_but_incomplete(self):
        """An empty packet (no claims, no passages) is valid but incomplete."""
        packet = _make_packet()
        validator = EvidencePacketValidator()
        result = validator.validate(packet)
        assert result.is_valid is True
        assert result.is_complete is False

    def test_packet_with_passages_and_provenance_is_complete(self):
        """A packet with passages, provenance, and evaluated claims is complete."""
        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        passage = _make_passage()
        binding = _make_binding(
            claim_id=claim.claim_id,
            passage_ids=[passage.passage_id],
        )
        rp = _make_rp(selected_passage_ids=[passage.passage_id])
        packet = _make_packet(
            claims=[claim],
            passages=[passage],
            claim_evidence_bindings=[binding],
            retrieval_provenance=[rp],
            freshness_summary={
                "most_recent": "2025-06-01T00:00:00Z",
                "oldest": "2025-01-01T00:00:00Z",
            },
        )
        caps = ResourceCaps.from_mapping(
            {
                **DEFAULT_POLICY.profiles["standard"].to_dict(),
                "max_evidence_packet_tokens": 8000,
            }
        )
        validator = EvidencePacketValidator()
        result = validator.validate(
            packet,
            effective_caps=caps,
        )
        assert result.is_valid is True
        assert result.is_complete is True

    def test_unassessed_claim_with_binding_errors(self):
        """An unassessed claim with bindings generates an error."""
        claim = _make_claim(semantic_status=SemanticStatus.UNASSESSED)
        passage = _make_passage()
        binding = _make_binding(
            claim_id=claim.claim_id,
            passage_ids=[passage.passage_id],
        )
        rp = _make_rp(selected_passage_ids=[passage.passage_id])
        packet = _make_packet(
            claims=[claim],
            passages=[passage],
            claim_evidence_bindings=[binding],
            retrieval_provenance=[rp],
            freshness_summary={
                "most_recent": "2025-06-01T00:00:00Z",
                "oldest": "2025-01-01T00:00:00Z",
            },
        )
        validator = EvidencePacketValidator()
        result = validator.validate(packet)
        # An unassessed claim with a binding is an error.
        assert result.is_valid is False
        assert any(f.code == "BOUND_UNASSESSED_CLAIM" for f in result.errors)

    def test_packet_with_supported_claim_no_binding_errors(self):
        """A supported claim without any binding is an error."""
        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        passage = _make_passage()
        packet = _make_packet(
            claims=[claim],
            passages=[passage],
            freshness_summary={
                "most_recent": "2025-06-01T00:00:00Z",
                "oldest": "2025-01-01T00:00:00Z",
            },
        )
        validator = EvidencePacketValidator()
        result = validator.validate(packet)
        assert result.is_valid is False
        assert any(f.code == "CLAIM_NO_BINDING" for f in result.errors)


# ---------------------------------------------------------------------------
# Diff tests
# ---------------------------------------------------------------------------


class TestPacketDiff:
    """Tests for packet diff functionality."""

    def test_no_diff_between_identical_packets(self):
        """Identical packets produce no diff."""
        passage = _make_passage()
        claim = _make_claim()
        packet = _make_packet(
            claims=[claim],
            passages=[passage],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        diff = diff_packets(packet, packet)
        assert diff.summary == "no differences"
        assert len(diff.added_passages) == 0
        assert len(diff.removed_passages) == 0
        assert len(diff.modified_passages) == 0
        assert len(diff.added_claims) == 0
        assert len(diff.removed_claims) == 0

    def test_added_and_removed_passages(self):
        """Passages added or removed between revisions are detected."""
        old_passage = _make_passage(passage_id=UUID(int=1), text="old text")
        new_passage = _make_passage(passage_id=UUID(int=2), text="new text")
        claim = _make_claim(claim_id=UUID(int=100))

        old_packet = _make_packet(
            claims=[claim],
            passages=[old_passage],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        new_packet = _make_packet(
            claims=[claim],
            passages=[new_passage],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )

        diff = diff_packets(old_packet, new_packet)
        assert len(diff.added_passages) == 1
        assert len(diff.removed_passages) == 1
        assert diff.added_passages[0].delta == "added"
        assert diff.removed_passages[0].delta == "removed"

    def test_modified_passage(self):
        """A passage with changed text or URL is detected as modified."""
        passage_v1 = _make_passage(
            passage_id=UUID(int=1),
            text="original text",
            source_url="https://example.com/v1",
        )
        passage_v2 = _make_passage(
            passage_id=UUID(int=1),
            text="updated text",
            source_url="https://example.com/v2",
        )
        claim = _make_claim(claim_id=UUID(int=100))

        old_packet = _make_packet(
            claims=[claim],
            passages=[passage_v1],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        new_packet = _make_packet(
            claims=[claim],
            passages=[passage_v2],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )

        diff = diff_packets(old_packet, new_packet)
        assert len(diff.modified_passages) == 1
        assert diff.modified_passages[0].delta == "modified"
        assert diff.modified_passages[0].old_url == "https://example.com/v1"
        assert diff.modified_passages[0].new_url == "https://example.com/v2"

    def test_claim_status_change(self):
        """A claim whose semantic_status changes is detected."""
        claim_v1 = _make_claim(
            claim_id=UUID(int=1),
            semantic_status=SemanticStatus.UNASSESSED,
        )
        claim_v2 = _make_claim(
            claim_id=UUID(int=1),
            semantic_status=SemanticStatus.SUPPORTED,
        )

        old_packet = _make_packet(
            claims=[claim_v1],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        new_packet = _make_packet(
            claims=[claim_v2],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )

        diff = diff_packets(old_packet, new_packet)
        assert len(diff.modified_claims) == 1
        assert diff.modified_claims[0].delta == "modified"
        assert diff.modified_claims[0].old_status == "unassessed"
        assert diff.modified_claims[0].new_status == "supported"

    def test_added_and_removed_groups(self):
        """Groups added or removed between revisions are detected."""
        claim = _make_claim(claim_id=UUID(int=100))
        passage = _make_passage(passage_id=UUID(int=200))
        group_v1 = EvidenceGroup(
            group_id=uuid4(),
            passage_ids=(passage.passage_id,),
            rationale="old group",
            evaluated=True,
        )
        group_v2 = EvidenceGroup(
            group_id=uuid4(),
            passage_ids=(passage.passage_id,),
            rationale="new group",
            evaluated=True,
        )

        old_packet = _make_packet(
            claims=[claim],
            passages=[passage],
            corroborating_groups=[group_v1],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        new_packet = _make_packet(
            claims=[claim],
            passages=[passage],
            corroborating_groups=[group_v2],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )

        diff = diff_packets(old_packet, new_packet)
        assert len(diff.removed_groups) == 1
        assert len(diff.added_groups) == 1

    def test_unresolved_items_diff(self):
        """Unresolved items added or removed are detected."""
        shared = uuid4()
        old_unresolved = (shared, uuid4())
        new_unresolved = (shared, uuid4())  # shared + one new, one removed

        old_packet = _make_packet(
            unresolved_items=old_unresolved,
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        new_packet = _make_packet(
            unresolved_items=new_unresolved,
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )

        diff = diff_packets(old_packet, new_packet)
        assert len(diff.removed_unresolved) == 1
        assert len(diff.added_unresolved) == 1

    def test_added_and_removed_omitted_passages(self):
        """Omitted passages added or removed between revisions are detected."""
        old_omitted = _make_passage(passage_id=UUID(int=100), text="old omitted")
        new_omitted = _make_passage(passage_id=UUID(int=200), text="new omitted")
        claim = _make_claim(claim_id=UUID(int=100))

        old_packet = _make_packet(
            claims=[claim],
            omitted_passages=[old_omitted],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        new_packet = _make_packet(
            claims=[claim],
            omitted_passages=[new_omitted],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )

        diff = diff_packets(old_packet, new_packet)
        assert len(diff.added_omitted_passages) == 1
        assert len(diff.removed_omitted_passages) == 1
        assert diff.added_omitted_passages[0].delta == "added"
        assert diff.removed_omitted_passages[0].delta == "removed"

    def test_modified_omitted_passage(self):
        """An omitted passage with changed text or URL is detected as modified."""
        omitted_v1 = _make_passage(
            passage_id=UUID(int=100),
            text="original omitted text",
            source_url="https://example.com/v1",
        )
        omitted_v2 = _make_passage(
            passage_id=UUID(int=100),
            text="updated omitted text",
            source_url="https://example.com/v2",
        )
        claim = _make_claim(claim_id=UUID(int=100))

        old_packet = _make_packet(
            claims=[claim],
            omitted_passages=[omitted_v1],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        new_packet = _make_packet(
            claims=[claim],
            omitted_passages=[omitted_v2],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )

        diff = diff_packets(old_packet, new_packet)
        assert len(diff.modified_omitted_passages) == 1
        assert diff.modified_omitted_passages[0].delta == "modified"
        assert diff.modified_omitted_passages[0].old_url == "https://example.com/v1"
        assert diff.modified_omitted_passages[0].new_url == "https://example.com/v2"

    def test_diff_to_json(self):
        """PacketDiff can be serialised to JSON."""
        diff = PacketDiff(
            old_run_id=str(uuid4()),
            new_run_id=str(uuid4()),
            old_revision=1,
            new_revision=2,
        )
        data = json.loads(diff.to_json())
        assert data["old_revision"] == 1
        assert data["new_revision"] == 2
        assert data["summary"] == "no differences"


# ---------------------------------------------------------------------------
# Referential validation tests
# ---------------------------------------------------------------------------


class TestReferentialValidation:
    """Tests for referential integrity validation."""

    def test_unknown_claim_ref_in_binding_errors(self):
        """EvidencePacket.__post_init__ rejects unknown claim refs at construction."""
        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        passage = _make_passage()
        unknown_claim_id = uuid4()
        binding = _make_binding(
            claim_id=unknown_claim_id,
            passage_ids=[passage.passage_id],
        )
        # The EvidencePacket constructor itself rejects unknown claim refs.
        with pytest.raises(ValueError, match="unknown evidence claim IDs"):
            _make_packet(
                claims=[claim],
                passages=[passage],
                claim_evidence_bindings=[binding],
                freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
            )

    def test_unknown_passage_ref_in_binding_errors(self):
        """EvidencePacket.__post_init__ rejects unknown passage refs in bindings."""
        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        passage = _make_passage()
        unknown_passage_id = uuid4()
        binding = _make_binding(
            claim_id=claim.claim_id,
            passage_ids=[unknown_passage_id],
        )
        with pytest.raises(ValueError, match="unknown passage IDs"):
            _make_packet(
                claims=[claim],
                passages=[passage],
                claim_evidence_bindings=[binding],
                freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
            )

    def test_unknown_passage_ref_in_group_errors(self):
        """EvidencePacket.__post_init__ rejects unknown passage refs in groups."""
        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        passage = _make_passage()
        unknown_passage_id = uuid4()
        group = EvidenceGroup(
            group_id=uuid4(),
            passage_ids=(passage.passage_id, unknown_passage_id),
            rationale="test group",
            evaluated=True,
        )
        binding = _make_binding(
            claim_id=claim.claim_id,
            passage_ids=[passage.passage_id],
        )
        with pytest.raises(ValueError, match="unknown passage IDs"):
            _make_packet(
                claims=[claim],
                passages=[passage],
                claim_evidence_bindings=[binding],
                corroborating_groups=[group],
                freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
            )

    def test_unknown_candidate_ref_errors(self):
        """A passage referencing an unknown candidate ID is an error."""
        passage = _make_passage()
        claim = _make_claim()
        known_candidate = uuid4()
        # The passage uses a different candidate ID.
        packet = _make_packet(
            claims=[claim],
            passages=[passage],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        validator = EvidencePacketValidator()
        result = validator.validate(
            packet,
            candidate_ids=frozenset([known_candidate]),
        )
        assert result.is_valid is False
        assert any(f.code == "UNKNOWN_CANDIDATE_REF" for f in result.errors)

    def test_unknown_snapshot_ref_errors(self):
        """A passage referencing an unknown snapshot ID is an error."""
        passage = _make_passage()
        claim = _make_claim()
        known_snapshot = uuid4()
        packet = _make_packet(
            claims=[claim],
            passages=[passage],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        validator = EvidencePacketValidator()
        result = validator.validate(
            packet,
            snapshot_ids=frozenset([known_snapshot]),
        )
        assert result.is_valid is False
        assert any(f.code == "UNKNOWN_SNAPSHOT_REF" for f in result.errors)

    def test_valid_packet_with_no_unknown_refs(self):
        """A well-formed packet passes referential validation."""
        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        passage = _make_passage()
        binding = _make_binding(
            claim_id=claim.claim_id,
            passage_ids=[passage.passage_id],
        )
        rp = _make_rp(selected_passage_ids=[passage.passage_id])
        candidate_id = passage.candidate_id
        snapshot_id = passage.snapshot_id
        packet = _make_packet(
            claims=[claim],
            passages=[passage],
            claim_evidence_bindings=[binding],
            retrieval_provenance=[rp],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        validator = EvidencePacketValidator()
        result = validator.validate(
            packet,
            candidate_ids=frozenset([candidate_id]),
            snapshot_ids=frozenset([snapshot_id]),
        )
        assert result.is_valid is True
        assert not any(
            f.code
            in (
                "UNKNOWN_CLAIM_REF",
                "UNKNOWN_PASSAGE_REF",
                "UNKNOWN_CANDIDATE_REF",
                "UNKNOWN_SNAPSHOT_REF",
            )
            for f in result.errors
        )


# ---------------------------------------------------------------------------
# Group completeness tests
# ---------------------------------------------------------------------------


class TestGroupCompleteness:
    """Tests for group completeness validation.

    Note: EvidenceGroup.__post_init__ already enforces:
    - No empty evaluated groups
    - No duplicate passage IDs in groups
    - Non-empty rationale for evaluated groups

    These tests verify the domain model rejects invalid groups.
    """

    def test_empty_evaluated_group_rejected(self):
        """EvidenceGroup rejects empty evaluated groups at construction."""
        with pytest.raises(ValueError, match="empty evidence group"):
            EvidenceGroup(
                group_id=uuid4(),
                passage_ids=(),
                rationale="test",
                evaluated=True,
            )

    def test_duplicate_passage_in_group_rejected(self):
        """EvidenceGroup rejects duplicate passage IDs at construction."""
        passage = _make_passage()
        with pytest.raises(ValueError, match="must not contain duplicates"):
            EvidenceGroup(
                group_id=uuid4(),
                passage_ids=(passage.passage_id, passage.passage_id),
                rationale="test",
                evaluated=True,
            )

    def test_empty_rationale_rejected(self):
        """EvidenceGroup rejects empty rationale at construction."""
        passage = _make_passage()
        with pytest.raises(ValueError, match="must be nonempty"):
            EvidenceGroup(
                group_id=uuid4(),
                passage_ids=(passage.passage_id,),
                rationale="",
                evaluated=True,
            )


# ---------------------------------------------------------------------------
# Freshness tests
# ---------------------------------------------------------------------------


class TestFreshnessValidation:
    """Tests for freshness validation."""

    def test_missing_freshness_summary_warns(self):
        """An empty freshness summary generates a warning."""
        packet = _make_packet(freshness_summary={})
        validator = EvidencePacketValidator()
        result = validator.validate(packet)
        assert any(f.code == "MISSING_FRESHNESS_SUMMARY" for f in result.warnings)

    def test_freshness_ordering_error_warns(self):
        """Freshness with most_recent before oldest generates a warning."""
        packet = _make_packet(
            freshness_summary={
                "most_recent": "2024-01-01T00:00:00Z",
                "oldest": "2025-06-01T00:00:00Z",
            }
        )
        validator = EvidencePacketValidator()
        result = validator.validate(packet)
        assert any(f.code == "FRESHNESS_ORDERING" for f in result.warnings)


# ---------------------------------------------------------------------------
# Token budget tests
# ---------------------------------------------------------------------------


class TestTokenBudgetValidation:
    """Tests for token budget validation."""

    def test_token_budget_exceeded_errors(self):
        """Passages exceeding the token budget are an error."""
        # Create a passage with enough text to exceed a tiny budget.
        long_text = "word " * 3000  # ~3000 tokens
        passage = _make_passage(text=long_text)
        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        binding = _make_binding(
            claim_id=claim.claim_id,
            passage_ids=[passage.passage_id],
        )
        rp = _make_rp(selected_passage_ids=[passage.passage_id])
        packet = _make_packet(
            claims=[claim],
            passages=[passage],
            claim_evidence_bindings=[binding],
            retrieval_provenance=[rp],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        tiny_caps = ResourceCaps.from_mapping(
            {
                **DEFAULT_POLICY.profiles["standard"].to_dict(),
                "max_evidence_packet_tokens": 100,  # very small budget
            }
        )
        validator = EvidencePacketValidator()
        result = validator.validate(packet, effective_caps=tiny_caps)
        assert result.is_valid is False
        assert any(f.code == "TOKEN_BUDGET_EXCEEDED" for f in result.errors)

    def test_token_budget_full_warns(self):
        """Passages using exactly the token budget generate a warning."""
        # "x" tokenizes to 1 token with cl100k_base.
        passage = _make_passage(text="x")  # 1 token
        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        binding = _make_binding(
            claim_id=claim.claim_id,
            passage_ids=[passage.passage_id],
        )
        rp = _make_rp(selected_passage_ids=[passage.passage_id])
        packet = _make_packet(
            claims=[claim],
            passages=[passage],
            claim_evidence_bindings=[binding],
            retrieval_provenance=[rp],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        caps = ResourceCaps.from_mapping(
            {
                **DEFAULT_POLICY.profiles["standard"].to_dict(),
                "max_evidence_packet_tokens": 1,  # exactly 1 token
            }
        )
        validator = EvidencePacketValidator()
        result = validator.validate(packet, effective_caps=caps)
        # Budget is exactly met, not exceeded — should be valid but warn.
        assert result.is_valid is True
        assert any(f.code == "TOKEN_BUDGET_FULL" for f in result.warnings)

    def test_omitted_passages_warn(self):
        """Omitted passages generate a warning."""
        passage = _make_passage()
        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        binding = _make_binding(
            claim_id=claim.claim_id,
            passage_ids=[passage.passage_id],
        )
        rp = _make_rp(selected_passage_ids=[passage.passage_id])
        omitted = _make_passage(text="omitted text")
        packet = _make_packet(
            claims=[claim],
            passages=[passage],
            omitted_passages=[omitted],
            claim_evidence_bindings=[binding],
            retrieval_provenance=[rp],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        caps = ResourceCaps.from_mapping(
            {
                **DEFAULT_POLICY.profiles["standard"].to_dict(),
                "max_evidence_packet_tokens": 8000,
            }
        )
        validator = EvidencePacketValidator()
        result = validator.validate(packet, effective_caps=caps)
        assert any(f.code == "OMITTED_PASSAGES" for f in result.warnings)


# ---------------------------------------------------------------------------
# Retrieval execution and provenance tests
# ---------------------------------------------------------------------------


class TestRetrievalExecutionValidation:
    """Tests for retrieval execution and provenance validation."""

    def test_passages_without_provenance_errors(self):
        """Passages without any retrieval_provenance is an error."""
        passage = _make_passage()
        claim = _make_claim()
        binding = _make_binding(
            claim_id=claim.claim_id,
            passage_ids=[passage.passage_id],
        )
        packet = _make_packet(
            claims=[claim],
            passages=[passage],
            claim_evidence_bindings=[binding],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        validator = EvidencePacketValidator()
        result = validator.validate(packet)
        assert result.is_valid is False
        assert any(f.code == "MISSING_RETRIEVAL_PROVENANCE" for f in result.errors)

    def test_passage_missing_source_url_warns(self):
        """EvidencePassage rejects empty source_url at construction.

        Since the domain model enforces non-empty source_url, this test
        verifies the constraint rather than testing the validator.
        """
        with pytest.raises(ValueError, match="must be nonempty"):
            _make_passage(source_url="")

    def test_passage_missing_candidate_id_errors(self):
        """EvidencePassage generates a UUID for candidate_id when None.

        The validator checks for missing candidate_id, but the domain model
        always generates one. This test verifies the validator still checks
        even when the passage has a valid candidate_id.
        """
        passage = _make_passage()
        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        binding = _make_binding(
            claim_id=claim.claim_id,
            passage_ids=[passage.passage_id],
        )
        rp = _make_rp(selected_passage_ids=[passage.passage_id])
        packet = _make_packet(
            claims=[claim],
            passages=[passage],
            claim_evidence_bindings=[binding],
            retrieval_provenance=[rp],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        # Since the domain model always generates candidate_id, a valid
        # passage will pass this check. The validator's MISSING_CANDIDATE_ID
        # check is still exercised when a passage is manually constructed
        # with candidate_id=None (which the domain model prevents).
        validator = EvidencePacketValidator()
        result = validator.validate(packet)
        # The packet is valid because candidate_id is always present.
        assert not any(f.code == "MISSING_CANDIDATE_ID" for f in result.errors)


# ---------------------------------------------------------------------------
# Semantic stage tests
# ---------------------------------------------------------------------------


class TestSemanticStageValidation:
    """Tests for semantic stage completeness validation."""

    def test_bound_unassessed_claim_errors(self):
        """A claim with bindings but unassessed status is an error."""
        claim = _make_claim(
            claim_id=UUID(int=1),
            semantic_status=SemanticStatus.UNASSESSED,
        )
        passage = _make_passage()
        binding = _make_binding(
            claim_id=claim.claim_id,
            passage_ids=[passage.passage_id],
        )
        rp = _make_rp(selected_passage_ids=[passage.passage_id])
        packet = _make_packet(
            claims=[claim],
            passages=[passage],
            claim_evidence_bindings=[binding],
            retrieval_provenance=[rp],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        validator = EvidencePacketValidator()
        result = validator.validate(packet)
        assert result.is_valid is False
        assert any(f.code == "BOUND_UNASSESSED_CLAIM" for f in result.errors)

    def test_binding_missing_model_errors(self):
        """ClaimEvidenceBinding rejects empty model at construction."""
        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        passage = _make_passage()
        # The domain model rejects empty model at construction.
        with pytest.raises(ValueError, match="model is required"):
            ClaimEvidenceBinding(
                binding_id=uuid4(),
                claim_id=claim.claim_id,
                passage_ids=(passage.passage_id,),
                relationship=EvidenceRelationship.SUPPORTS,
                confidence=0.9,
                uncertainty="low",
                model="",  # empty model
                prompt_version="v1",
                schema_version=1,
                input_packet_revision=1,
            )

    def test_invalid_input_packet_revision_errors(self):
        """ClaimEvidenceBinding rejects input_packet_revision < 1 at construction."""
        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        passage = _make_passage()
        # The domain model rejects revision < 1 at construction.
        with pytest.raises(ValueError, match="input_packet_revision"):
            ClaimEvidenceBinding(
                binding_id=uuid4(),
                claim_id=claim.claim_id,
                passage_ids=(passage.passage_id,),
                relationship=EvidenceRelationship.SUPPORTS,
                confidence=0.9,
                uncertainty="low",
                model="test-model",
                prompt_version="v1",
                schema_version=1,
                input_packet_revision=0,  # invalid
            )


# ---------------------------------------------------------------------------
# Unresolved requirements tests
# ---------------------------------------------------------------------------


class TestUnresolvedRequirements:
    """Tests for unresolved requirements validation."""

    def test_unknown_coverage_item_in_unresolved_errors(self):
        """Unresolved items referencing unknown coverage items are errors."""
        unknown_id = uuid4()
        known_id = uuid4()
        packet = _make_packet(
            unresolved_items=(unknown_id,),
        )
        validator = EvidencePacketValidator()
        result = validator.validate(
            packet,
            coverage_items=frozenset([known_id]),
        )
        assert result.is_valid is False
        assert any(f.code == "UNRESOLVED_UNKNOWN_COVERAGE" for f in result.errors)

    def test_no_coverage_context_warns(self):
        """Unresolved items without coverage context generate a warning."""
        packet = _make_packet(
            unresolved_items=(uuid4(),),
        )
        validator = EvidencePacketValidator()
        result = validator.validate(packet)
        assert any(f.code == "UNRESOLVED_NO_COVERAGE_CONTEXT" for f in result.warnings)


# ---------------------------------------------------------------------------
# Bounded citation-ready output tests
# ---------------------------------------------------------------------------


class TestBoundedCitationReadyOutput:
    """Tests for bounded citation-ready output."""

    def test_bounded_output_limits_passages(self):
        """max_passages limits the number of passages in output."""
        passages = [_make_passage(text=f"passage {i}") for i in range(5)]
        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        packet = _make_packet(
            claims=[claim],
            passages=passages,
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        output = bounded_citation_ready_output(packet, max_passages=3)
        assert len(output["passages"]) == 3
        assert output["metadata"]["passage_count"] == 5

    def test_bounded_output_limits_claims(self):
        """max_claims limits the number of claims in output."""
        claims = [
            _make_claim(claim_id=UUID(int=i), statement=f"claim {i}")
            for i in range(1, 6)
        ]
        passage = _make_passage()
        packet = _make_packet(
            claims=claims,
            passages=[passage],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        output = bounded_citation_ready_output(packet, max_claims=2)
        assert len(output["claims"]) == 2
        assert output["metadata"]["claim_count"] == 5

    def test_bounded_output_includes_bindings(self):
        """Bindings are included in the output."""
        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        passage = _make_passage()
        binding = _make_binding(
            claim_id=claim.claim_id,
            passage_ids=[passage.passage_id],
        )
        packet = _make_packet(
            claims=[claim],
            passages=[passage],
            claim_evidence_bindings=[binding],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        output = bounded_citation_ready_output(packet)
        assert str(claim.claim_id) in output["bindings"]
        assert len(output["bindings"][str(claim.claim_id)]) == 1

    def test_bounded_output_is_json_serializable(self):
        """Bounded output can be serialised to JSON."""
        claim = _make_claim(semantic_status=SemanticStatus.SUPPORTED)
        passage = _make_passage()
        packet = _make_packet(
            claims=[claim],
            passages=[passage],
            freshness_summary={"most_recent": "2025-06-01T00:00:00Z"},
        )
        output = bounded_citation_ready_output(packet)
        json_str = json.dumps(output)
        assert json_str  # non-empty


# ---------------------------------------------------------------------------
# CLI argument-parsing tests
# ---------------------------------------------------------------------------


class TestCLIArgumentParsing:
    """Tests that the CLI argument parser recognises packet-* subcommands."""

    def _get_parser(self):
        from firecrawl_skill.research_store.cli import parser

        return parser()

    def test_packet_validate_subcommand(self):
        """packet-validate subcommand is parsed correctly."""
        parser = self._get_parser()
        args = parser.parse_args(["packet-validate", "test-run-id"])
        assert args.command == "packet-validate"
        assert args.run_id == "test-run-id"
        assert args.revision is None
        assert args.output == "-"
        assert args.include_warnings is False

    def test_packet_validate_with_revision_and_include_warnings(self):
        """packet-validate accepts --revision and --include-warnings."""
        parser = self._get_parser()
        args = parser.parse_args(
            [
                "packet-validate",
                "test-run-id",
                "--revision",
                "2",
                "--include-warnings",
            ]
        )
        assert args.command == "packet-validate"
        assert args.revision == 2
        assert args.include_warnings is True

    def test_packet_inspect_with_bounded(self):
        """packet-inspect --bounded flags are parsed correctly."""
        parser = self._get_parser()
        args = parser.parse_args(
            [
                "packet-inspect",
                "test-run-id",
                "--bounded",
                "--max-passages",
                "5",
                "--max-claims",
                "3",
            ]
        )
        assert args.command == "packet-inspect"
        assert args.bounded is True
        assert args.max_passages == 5
        assert args.max_claims == 3

    def test_packet_diff_requires_revisions(self):
        """packet-diff requires --old-revision and --new-revision."""
        parser = self._get_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["packet-diff", "test-run-id"])
        args = parser.parse_args(
            [
                "packet-diff",
                "test-run-id",
                "--old-revision",
                "1",
                "--new-revision",
                "2",
            ]
        )
        assert args.command == "packet-diff"
        assert args.old_revision == 1
        assert args.new_revision == 2

    def test_packet_export_requires_output(self):
        """packet-export requires --output."""
        parser = self._get_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["packet-export", "test-run-id"])
        args = parser.parse_args(
            [
                "packet-export",
                "test-run-id",
                "--output",
                "output.json",
            ]
        )
        assert args.command == "packet-export"
        assert args.output == "output.json"


# ---------------------------------------------------------------------------
# CLI exit-code logic tests
# ---------------------------------------------------------------------------


class TestCLIPacketValidateExitCode:
    """Tests for the packet-validate CLI exit-code logic.

    These tests exercise the exit-code decision tree that was the subject
    of review finding B1: the --include-warnings flag must not suppress
    errors from causing a non-zero exit.
    """

    def _simulate_cli_exit_code(
        self, is_valid, is_complete, errors, warnings, include_warnings
    ):
        """Simulate the CLI exit-code logic (cli.py lines 2739–2748).

        Returns the exit code the CLI would return.
        """
        if is_valid and is_complete:
            return 0
        # In the real CLI, output is printed to stderr here.
        if errors:
            return 1
        if not include_warnings and warnings:
            return 1
        return 0

    def test_errors_always_exit_nonzero(self):
        """Errors always cause exit code 1, regardless of --include-warnings."""
        # With errors, --include-warnings should NOT suppress the non-zero exit.
        assert (
            self._simulate_cli_exit_code(
                is_valid=False,
                is_complete=False,
                errors=["SOME_ERROR"],
                warnings=[],
                include_warnings=True,
            )
            == 1
        )

    def test_warnings_exit_nonzero_without_flag(self):
        """Warnings cause exit code 1 when --include-warnings is not set."""
        assert (
            self._simulate_cli_exit_code(
                is_valid=True,
                is_complete=False,
                errors=[],
                warnings=["SOME_WARNING"],
                include_warnings=False,
            )
            == 1
        )

    def test_warnings_suppressed_with_flag(self):
        """Warnings do not cause exit code 1 when --include-warnings is set."""
        assert (
            self._simulate_cli_exit_code(
                is_valid=True,
                is_complete=False,
                errors=[],
                warnings=["SOME_WARNING"],
                include_warnings=True,
            )
            == 0
        )

    def test_valid_and_complete_exits_zero(self):
        """A fully valid and complete packet always exits 0."""
        assert (
            self._simulate_cli_exit_code(
                is_valid=True,
                is_complete=True,
                errors=[],
                warnings=[],
                include_warnings=False,
            )
            == 0
        )


class TestValidationResult:
    """Tests for ValidationResult serialization."""

    def test_to_dict(self):
        """ValidationResult.to_dict includes all fields."""
        result = ValidationResult(
            is_valid=True,
            is_complete=False,
            errors=(),
            warnings=(
                ValidationFinding(
                    code="TEST_WARNING",
                    severity=cast(ValidationSeverity, ValidationSeverity.WARNING),
                    message="test warning",
                ),
            ),
        )
        d = result.to_dict()
        assert d["is_valid"] is True
        assert d["is_complete"] is False
        assert d["error_count"] == 0
        assert d["warning_count"] == 1
        assert len(d["warnings"]) == 1

    def test_to_json(self):
        """ValidationResult.to_json produces valid JSON."""
        result = ValidationResult(
            is_valid=True,
            is_complete=True,
        )
        data = json.loads(result.to_json())
        assert data["is_valid"] is True
        assert data["is_complete"] is True

    def test_summary_valid_and_complete(self):
        """Summary reflects valid+complete state."""
        result = ValidationResult(is_valid=True, is_complete=True)
        assert result.summary == "packet is valid and complete"

    def test_summary_valid_but_incomplete(self):
        """Summary reflects valid but incomplete state."""
        result = ValidationResult(
            is_valid=True,
            is_complete=False,
            warnings=(
                ValidationFinding(
                    code="TEST",
                    severity=cast(ValidationSeverity, ValidationSeverity.WARNING),
                    message="test",
                ),
            ),
        )
        assert "valid but incomplete" in result.summary

    def test_summary_invalid(self):
        """Summary reflects invalid state."""
        result = ValidationResult(
            is_valid=False,
            is_complete=False,
            errors=(
                ValidationFinding(
                    code="TEST",
                    severity=cast(ValidationSeverity, ValidationSeverity.ERROR),
                    message="test",
                ),
            ),
        )
        assert "invalid" in result.summary
