"""Evidence grouping engine for corroboration, contradiction, and qualification."""

from __future__ import annotations

import logging
from collections import defaultdict
from uuid import UUID, uuid4

from research_domain.models import (
    EvidenceGroup,
    EvidencePacket,
    EvidencePassage,
    EvidenceRelationship,
)

logger = logging.getLogger(__name__)


class EvidenceGroupingService:
    """Populate corroboration, contradiction, and qualification groups."""

    def group_evidence(
        self,
        packet: EvidencePacket,
    ) -> dict[str, tuple[EvidenceGroup, ...]]:
        passages_by_id: dict[UUID, EvidencePassage] = {}
        for passage in packet.passages:
            passages_by_id[passage.passage_id] = passage
        for passage in packet.omitted_passages:
            passages_by_id[passage.passage_id] = passage

        bindings_by_claim: dict[UUID, list] = defaultdict(list)
        for binding in packet.claim_evidence_bindings:
            bindings_by_claim[binding.claim_id].append(binding)

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
        processed_claims: set[UUID] = set()

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

        for claim in packet.claims:
            if claim.claim_id in processed_claims:
                continue
            status = claim.semantic_status
            if status in ("unsupported", "unassessed", "uncertain"):
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
                corroborating.append(
                    EvidenceGroup(
                        group_id=uuid4(),
                        passage_ids=(),
                        rationale=(
                            f"Claim '{claim.statement}' is supported but no passage "
                            "bindings were produced; evaluated absence."
                        ),
                        evaluated=False,
                    )
                )
            elif status == "contradicted":
                contradicting.append(
                    EvidenceGroup(
                        group_id=uuid4(),
                        passage_ids=(),
                        rationale=(
                            f"Claim '{claim.statement}' is contradicted but no "
                            "contradicting bindings were produced; evaluated absence."
                        ),
                        evaluated=False,
                    )
                )
            elif status == "qualified":
                qualifying.append(
                    EvidenceGroup(
                        group_id=uuid4(),
                        passage_ids=(),
                        rationale=(
                            f"Claim '{claim.statement}' is qualified but no qualifying "
                            "bindings were produced; evaluated absence."
                        ),
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
                "NOTE: Single-source corroboration; independent corroboration "
                "not yet established."
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
        passage_ids = tuple(binding.passage_ids)
        source_urls = self._extract_source_urls(passage_ids, passages_by_id)
        unique_sources = len(set(source_urls))
        rationale = (
            f"Claim '{claim_info.get('statement', '')}' is contradicted by "
            f"{unique_sources} source(s): "
            f"{', '.join(sorted(set(source_urls))) if source_urls else 'unknown'}. "
            f"Relationship: contradicts (confidence={binding.confidence}). "
            "Contradictory evidence preserved."
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
        passage_ids = tuple(binding.passage_ids)
        source_urls = self._extract_source_urls(passage_ids, passages_by_id)
        unique_sources = len(set(source_urls))
        rationale = (
            f"Claim '{claim_info.get('statement', '')}' is qualified by "
            f"{unique_sources} source(s): "
            f"{', '.join(sorted(set(source_urls))) if source_urls else 'unknown'}. "
            f"Relationship: qualifies (confidence={binding.confidence}). "
            "Contextual constraint recorded."
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
        urls = []
        for passage_id in passage_ids:
            passage = passages_by_id.get(passage_id)
            if passage and passage.source_url:
                urls.append(passage.source_url)
        return urls

    def build_packet_with_groups(self, packet: EvidencePacket) -> EvidencePacket:
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


__all__ = ["EvidenceGroupingService"]
