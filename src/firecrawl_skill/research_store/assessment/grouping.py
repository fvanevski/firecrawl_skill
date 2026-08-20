"""Evidence grouping engine for corroboration, contradiction, and qualification.

This module provides :class:`EvidenceGroupingService`, which populates the
``corroborating_groups``, ``contradicting_groups``, and ``qualifying_groups``
fields on an ``EvidencePacket`` by analysing the claim-evidence bindings
produced by :class:`ClaimBindingService`.

Grouping semantics
------------------
* **Corroborating** — bindings with relationship ``supports`` produce one
  corroborating group per binding.  The rationale records the number of
  distinct source URLs and flags single-source corroboration.
* **Contradicting** — bindings with relationship ``contradicts`` produce one
  contradicting group per binding.  Contradictory evidence is always
  preserved, never silently dropped.
* **Qualifying** — bindings with relationship ``qualifies`` produce one
  qualifying group per binding.  Qualifying groups capture contextual
  constraints on a claim.
* **Context** — bindings with relationship ``context`` are silently
  discarded.  They do not form a top-level group field in the current
  packet schema and are not returned by the grouping engine.

Evaluated absence
-----------------
When a claim has **no** bindings (regardless of its ``semantic_status``),
the grouping engine creates a single unevaluated group with empty
``passage_ids`` and ``evaluated=False``.  The rationale distinguishes
between:

- **Unevaluated** — the semantic model has not yet evaluated the claim
  (``unsupported``, ``unassessed``, ``uncertain``).
- **Evaluated absence** — the model evaluated the claim as
  ``supported``, ``contradicted``, or ``qualified`` but produced no
  bindings.

Source independence
-------------------
The service extracts source URLs from passages referenced by bindings and
notes in the rationale whether corroboration comes from a single source
or multiple independent sources.  It does not consult duplicate-group
assessments — that work is handled by :class:`DuplicateGroupService` and
is available on the ``EvidencePacket`` via ``near_duplicate_groups`` and
``independence_assessments``.

Return format
-------------
The service returns a dict compatible with the ``EvidencePacket`` schema:

    {
        "corroborating_groups": [EvidenceGroup, ...],
        "contradicting_groups": [EvidenceGroup, ...],
        "qualifying_groups": [EvidenceGroup, ...],
    }

Each group carries the exact ``passage_ids`` from the bindings and a
rationale that records the relationship type, claim statement, and
source URLs.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from uuid import UUID, uuid4

from firecrawl_skill.research_domain.models import (
    EvidenceGroup,
    EvidencePacket,
    EvidencePassage,
    EvidenceRelationship,
)

logger = logging.getLogger(__name__)


class EvidenceGroupingService:
    """Populate corroboration, contradiction, and qualification groups.

    This service is **pure-functional** and **in-memory only**.  It never
    touches the database.  The ``group_evidence`` method takes an
    ``EvidencePacket`` (with claim-evidence bindings) and returns a dict
    of ``EvidenceGroup`` tuples that the caller uses to construct a new
    ``EvidencePacket`` revision.
    """

    def group_evidence(
        self,
        packet: EvidencePacket,
    ) -> dict[str, tuple[EvidenceGroup, ...]]:
        """Group evidence passages by claim and relationship.

        Args:
            packet: An ``EvidencePacket`` that contains claims, passages,
                and ``claim_evidence_bindings``.

        Returns:
            A dict with keys ``corroborating_groups``,
            ``contradicting_groups``, ``qualifying_groups``, and
            ``context_groups``, each mapping to a tuple of
            ``EvidenceGroup`` instances.
        """
        passages_by_id: dict[UUID, EvidencePassage] = {}
        for passage in packet.passages:
            passages_by_id[passage.passage_id] = passage
        for passage in packet.omitted_passages:
            passages_by_id[passage.passage_id] = passage

        # Index bindings by claim_id
        bindings_by_claim: dict[UUID, list] = defaultdict(list)
        for binding in packet.claim_evidence_bindings:
            bindings_by_claim[binding.claim_id].append(binding)

        # Index claims for quick lookup
        claims_by_id: dict[UUID, dict] = {}
        for claim in packet.claims:
            claims_by_id[claim.claim_id] = {
                "statement": claim.statement,
                "semantic_status": claim.semantic_status,
            }

        corroborating: list[EvidenceGroup] = []
        contradicting: list[EvidenceGroup] = []
        qualifying: list[EvidenceGroup] = []
        context_groups: list[EvidenceGroup] = []

        # Track which claims have been processed
        processed_claims: set[UUID] = set()

        # Process bindings by relationship
        for binding in packet.claim_evidence_bindings:
            claim_id = binding.claim_id
            processed_claims.add(claim_id)

            if binding.relationship == EvidenceRelationship.SUPPORTS:
                corroborating.append(
                    self._make_support_group(
                        claim_id=claim_id,
                        claim_info=claims_by_id.get(claim_id, {}),
                        binding=binding,
                        passages_by_id=passages_by_id,
                    )
                )
            elif binding.relationship == EvidenceRelationship.CONTRADICTS:
                contradicting.append(
                    self._make_contradict_group(
                        claim_id=claim_id,
                        claim_info=claims_by_id.get(claim_id, {}),
                        binding=binding,
                        passages_by_id=passages_by_id,
                    )
                )
            elif binding.relationship == EvidenceRelationship.QUALIFIES:
                qualifying.append(
                    self._make_qualify_group(
                        claim_id=claim_id,
                        claim_info=claims_by_id.get(claim_id, {}),
                        binding=binding,
                        passages_by_id=passages_by_id,
                    )
                )
            elif binding.relationship == EvidenceRelationship.CONTEXT:
                context_groups.append(
                    self._make_context_group(
                        claim_id=claim_id,
                        claim_info=claims_by_id.get(claim_id, {}),
                        binding=binding,
                        passages_by_id=passages_by_id,
                    )
                )

        # Handle claims with no bindings (unevaluated vs evaluated absence)
        for claim in packet.claims:
            if claim.claim_id not in processed_claims:
                status = claim.semantic_status
                if status in ("unsupported", "unassessed", "uncertain"):
                    # Unevaluated — the semantic model has not yet evaluated
                    # this claim, or explicitly marked it unsupported.
                    rationale = (
                        f"Claim '{claim.statement}' is {status.value}; "
                        "no evidence bindings were produced."
                    )
                    corroborating.append(
                        EvidenceGroup(
                            group_id=uuid4(),
                            passage_ids=(),
                            rationale=rationale,
                            evaluated=False,
                        )
                    )
                elif status == "supported":
                    # Evaluated absence — the model said supported but
                    # produced no bindings.  Use evaluated=False so that
                    # EvidenceGroup.__post_init__ does not reject the empty
                    # passage_ids.  The rationale still distinguishes this
                    # from a truly unevaluated claim.
                    rationale = (
                        f"Claim '{claim.statement}' is supported but no "
                        "passage bindings were produced; evaluated absence."
                    )
                    corroborating.append(
                        EvidenceGroup(
                            group_id=uuid4(),
                            passage_ids=(),
                            rationale=rationale,
                            evaluated=False,
                        )
                    )
                elif status == "contradicted":
                    rationale = (
                        f"Claim '{claim.statement}' is contradicted but no "
                        "contradicting bindings were produced; evaluated absence."
                    )
                    contradicting.append(
                        EvidenceGroup(
                            group_id=uuid4(),
                            passage_ids=(),
                            rationale=rationale,
                            evaluated=False,
                        )
                    )
                elif status == "qualified":
                    rationale = (
                        f"Claim '{claim.statement}' is qualified but no "
                        "qualifying bindings were produced; evaluated absence."
                    )
                    qualifying.append(
                        EvidenceGroup(
                            group_id=uuid4(),
                            passage_ids=(),
                            rationale=rationale,
                            evaluated=False,
                        )
                    )

        return {
            "corroborating_groups": tuple(corroborating),
            "contradicting_groups": tuple(contradicting),
            "qualifying_groups": tuple(qualifying),
        }

    def _make_support_group(
        self,
        claim_id: UUID,
        claim_info: dict,
        binding,
        passages_by_id: dict,
    ) -> EvidenceGroup:
        """Create a corroborating group for a single binding."""
        passage_ids = tuple(binding.passage_ids)
        source_urls = self._extract_source_urls(passage_ids, passages_by_id)
        unique_sources = len(set(source_urls))

        if unique_sources > 1:
            rationale = (
                f"Claim '{claim_info.get('statement', '')}' is supported by "
                f"{unique_sources} independent source(s): "
                f"{', '.join(sorted(set(source_urls)))}. "
                f"Relationship: supports (confidence={binding.confidence})."
            )
        else:
            rationale = (
                f"Claim '{claim_info.get('statement', '')}' is supported by "
                f"1 source: {source_urls[0] if source_urls else 'unknown'}. "
                f"Relationship: supports (confidence={binding.confidence}). "
                f"NOTE: Single-source corroboration; independent corroboration "
                f"not yet established."
            )

        return EvidenceGroup(
            group_id=uuid4(),
            passage_ids=passage_ids,
            rationale=rationale,
            evaluated=True,
        )

    def _make_contradict_group(
        self,
        claim_id: UUID,
        claim_info: dict,
        binding,
        passages_by_id: dict,
    ) -> EvidenceGroup:
        """Create a contradicting group for a single binding."""
        passage_ids = tuple(binding.passage_ids)
        source_urls = self._extract_source_urls(passage_ids, passages_by_id)
        unique_sources = len(set(source_urls))

        rationale = (
            f"Claim '{claim_info.get('statement', '')}' is contradicted by "
            f"{unique_sources} source(s): "
            f"{', '.join(sorted(set(source_urls))) if source_urls else 'unknown'}. "
            f"Relationship: contradicts (confidence={binding.confidence}). "
            f"Contradictory evidence preserved."
        )

        return EvidenceGroup(
            group_id=uuid4(),
            passage_ids=passage_ids,
            rationale=rationale,
            evaluated=True,
        )

    def _make_qualify_group(
        self,
        claim_id: UUID,
        claim_info: dict,
        binding,
        passages_by_id: dict,
    ) -> EvidenceGroup:
        """Create a qualifying group for a single binding."""
        passage_ids = tuple(binding.passage_ids)
        source_urls = self._extract_source_urls(passage_ids, passages_by_id)
        unique_sources = len(set(source_urls))

        rationale = (
            f"Claim '{claim_info.get('statement', '')}' is qualified by "
            f"{unique_sources} source(s): "
            f"{', '.join(sorted(set(source_urls))) if source_urls else 'unknown'}. "
            f"Relationship: qualifies (confidence={binding.confidence}). "
            f"Contextual constraint recorded."
        )

        return EvidenceGroup(
            group_id=uuid4(),
            passage_ids=passage_ids,
            rationale=rationale,
            evaluated=True,
        )

    def _make_context_group(
        self,
        claim_id: UUID,
        claim_info: dict,
        binding,
        passages_by_id: dict,
    ) -> EvidenceGroup:
        """Create a context group for a single binding."""
        passage_ids = tuple(binding.passage_ids)
        source_urls = self._extract_source_urls(passage_ids, passages_by_id)
        unique_sources = len(set(source_urls))

        rationale = (
            f"Claim '{claim_info.get('statement', '')}' has contextual "
            f"information from {unique_sources} source(s): "
            f"{', '.join(sorted(set(source_urls))) if source_urls else 'unknown'}. "
            f"Relationship: context (confidence={binding.confidence})."
        )

        return EvidenceGroup(
            group_id=uuid4(),
            passage_ids=passage_ids,
            rationale=rationale,
            evaluated=True,
        )

    def _extract_source_urls(
        self,
        passage_ids: tuple[UUID, ...],
        passages_by_id: dict,
    ) -> list[str]:
        """Extract source URLs for a set of passage IDs."""
        urls = []
        for pid in passage_ids:
            passage = passages_by_id.get(pid)
            if passage and passage.source_url:
                urls.append(passage.source_url)
        return urls

    def build_packet_with_groups(
        self,
        packet: EvidencePacket,
    ) -> EvidencePacket:
        """Build a new EvidencePacket with grouping fields populated.

        Args:
            packet: The original EvidencePacket.

        Returns:
            A new EvidencePacket with
            ``corroborating_groups``, ``contradicting_groups``,
            and ``qualifying_groups`` populated from the claim-evidence
            bindings.  Bindings with ``context`` relationship are not
            included in the returned packet.
        """
        groups = self.group_evidence(packet)

        return EvidencePacket(
            schema_version=packet.schema_version,
            run_id=packet.run_id,
            research_spec_id=packet.research_spec_id,
            coverage_revision=packet.coverage_revision,
            claims=packet.claims,
            passages=packet.passages,
            omitted_passages=packet.omitted_passages,
            claim_evidence_bindings=packet.claim_evidence_bindings,
            corroborating_groups=groups["corroborating_groups"],
            contradicting_groups=groups["contradicting_groups"],
            qualifying_groups=groups["qualifying_groups"],
            near_duplicate_groups=packet.near_duplicate_groups,
            source_diversity_summary=packet.source_diversity_summary,
            freshness_summary=packet.freshness_summary,
            limitations=packet.limitations,
            unresolved_items=packet.unresolved_items,
            independence_assessments=packet.independence_assessments,
            retrieval_provenance=packet.retrieval_provenance,
        )
