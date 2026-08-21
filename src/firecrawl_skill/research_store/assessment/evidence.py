"""Canonical EvidencePacket construction and persistence boundary."""

from __future__ import annotations

import dataclasses
import datetime
import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from firecrawl_skill.research_domain.models import (
    EvidenceClaim,
    EvidenceGroup,
    EvidencePacket,
    EvidencePassage,
    IndependenceAssessment,
    IndependenceStatus,
    RetrievalProvenance,
)
from firecrawl_skill.research_domain.registry import load_model

from ..budget_policy import BudgetPolicy, ResourceCaps
from ..tokenizer_registry import get_tokenizer
from .duplicates import DuplicateGroupService
from .grouping import EvidenceGroupingService

logger = logging.getLogger(__name__)


def _to_dict(obj: Any) -> Any:
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj):
        return {k: _to_dict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    return obj


class EvidenceService:
    """Service for deterministic EvidencePacket construction and persistence."""

    def __init__(
        self,
        uow_factory: Callable[[], Any],
        budget_policy: BudgetPolicy,
        tokenizer_name: str = "cl100k_base",
    ):
        self.uow_factory = uow_factory
        self.budget_policy = budget_policy
        self.tokenizer = get_tokenizer(tokenizer_name)
        self.duplicate_service = DuplicateGroupService()
        self.grouping_service = EvidenceGroupingService()

    def build_evidence_packet(
        self,
        run_id: UUID,
        research_spec_id: UUID,
        coverage_revision: int,
        candidates: list[dict],
        retrieval_events: list[RetrievalProvenance],
        effective_caps: ResourceCaps,
        claims: tuple[EvidenceClaim, ...] = (),
    ) -> EvidencePacket:
        sorted_candidates = sorted(
            candidates,
            key=lambda c: (str(c.get("snapshot_id", "")), str(c.get("chunk_id", ""))),
        )

        passages = []
        omitted_passages = []
        token_count = 0
        max_tokens = effective_caps.max_evidence_packet_tokens
        source_domains = set()
        oldest = None
        newest = None

        for cand in sorted_candidates:
            raw_text = cand.get("text", cand.get("excerpt", ""))
            text = raw_text if isinstance(raw_text, str) else str(raw_text or "")
            cand_tokens = len(self.tokenizer.encode(text))
            source_url = cand.get("source_url") or cand.get("url") or ""
            passage = EvidencePassage(
                passage_id=UUID(str(cand["chunk_id"])),
                candidate_id=UUID(str(cand["candidate_id"])),
                snapshot_id=UUID(str(cand["snapshot_id"])),
                chunk_id=UUID(str(cand["chunk_id"])),
                text=text,
                source_url=source_url,
            )

            if token_count + cand_tokens <= max_tokens:
                if source_url:
                    source_domains.add(source_url)
                cand_date_str = cand.get("date")
                if cand_date_str:
                    try:
                        cand_date = datetime.datetime.fromisoformat(
                            cand_date_str.replace("Z", "+00:00")
                        )
                        if oldest is None or cand_date < oldest:
                            oldest = cand_date
                        if newest is None or cand_date > newest:
                            newest = cand_date
                    except ValueError:
                        pass
                passages.append(passage)
                token_count += cand_tokens
            else:
                omitted_passages.append(passage)

        diversity_summary = {
            "unique_sources": len(source_domains),
            "sources": sorted(source_domains),
        }
        freshness_summary = {
            "most_recent": newest.isoformat() if newest else None,
            "oldest": oldest.isoformat() if oldest else None,
        }

        near_duplicate_groups = []
        if omitted_passages:
            near_duplicate_groups.append(
                EvidenceGroup(
                    group_id=uuid4(),
                    passage_ids=tuple(p.passage_id for p in omitted_passages),
                    rationale="omitted_due_to_budget",
                    evaluated=False,
                )
            )

        result = self.duplicate_service.evaluate_candidates(candidates)
        dup_groups = result["groups"]
        unassessed_ids = result.get("unassessed", [])
        cand_to_passage = {
            p.candidate_id: p.passage_id for p in passages + omitted_passages
        }
        independence_assessments = []

        for group in dup_groups:
            group_passage_ids = []
            for candidate_id in group["candidate_ids"]:
                candidate_uuid = (
                    UUID(str(candidate_id))
                    if not isinstance(candidate_id, UUID)
                    else candidate_id
                )
                passage_id = cand_to_passage.get(candidate_uuid)
                if passage_id:
                    group_passage_ids.append(passage_id)

            if len(group_passage_ids) > 1:
                near_duplicate_groups.append(
                    EvidenceGroup(
                        group_id=group["group_id"],
                        passage_ids=tuple(group_passage_ids),
                        rationale=group["rationale"],
                        evaluated=True,
                    )
                )

            for candidate_id, assessment in group.get("assessments", {}).items():
                independence_assessments.append(
                    IndependenceAssessment(
                        candidate_id=candidate_id,
                        status=assessment["status"],
                        rationale=assessment["rationale"],
                    )
                )

        for candidate_id in unassessed_ids:
            independence_assessments.append(
                IndependenceAssessment(
                    candidate_id=UUID(str(candidate_id)),
                    status=IndependenceStatus.UNASSESSED,
                    rationale="no duplicate or syndication signal found",
                )
            )

        return EvidencePacket(
            schema_version=EvidencePacket.SCHEMA_VERSION,
            run_id=run_id,
            research_spec_id=research_spec_id,
            coverage_revision=coverage_revision,
            claims=claims,
            passages=tuple(passages),
            omitted_passages=tuple(omitted_passages),
            claim_evidence_bindings=(),
            corroborating_groups=(),
            contradicting_groups=(),
            qualifying_groups=(),
            near_duplicate_groups=tuple(near_duplicate_groups),
            source_diversity_summary=diversity_summary,
            freshness_summary=freshness_summary,
            limitations=(),
            unresolved_items=(),
            independence_assessments=tuple(independence_assessments),
            retrieval_provenance=tuple(retrieval_events),
        )

    def persist_packet(self, packet: EvidencePacket) -> int:
        with self.uow_factory() as uow:
            latest = uow.evidence_packets.get_evidence_packet(packet.run_id)
            revision = latest.packet_revision + 1 if latest else 1
            uow.evidence_packets.persist_evidence_packet(
                packet.run_id,
                packet.research_spec_id,
                packet.coverage_revision,
                revision,
                _to_dict(packet),
            )
            return revision

    def export_packet(self, run_id: UUID, revision: int | None = None) -> dict | None:
        with self.uow_factory() as uow:
            packet_record = uow.evidence_packets.get_evidence_packet(run_id, revision)
            return packet_record.to_dict() if packet_record else None

    def group_evidence(self, run_id: UUID, revision: int | None = None) -> int:
        with self.uow_factory() as uow:
            packet_record = uow.evidence_packets.get_evidence_packet(run_id, revision)
            if not packet_record:
                raise ValueError(
                    f"EvidencePacket {run_id} r{revision or 'latest'} not found"
                )

            packet_dict = getattr(packet_record, "payload", None)
            if not isinstance(packet_dict, dict):
                record_dict = packet_record.to_dict()
                packet_dict = record_dict.get("payload", record_dict)
            packet = load_model(packet_dict)

            if not packet.claim_evidence_bindings:
                logger.info(
                    "No claim-evidence bindings for %s r%s; skipping evidence grouping.",
                    run_id,
                    packet_record.packet_revision,
                )
                return packet_record.packet_revision

            try:
                new_packet = self.grouping_service.build_packet_with_groups(packet)
            except ValueError as exc:  # pragma: no cover
                logger.error(
                    "Evidence grouping failed for %s r%s: %s; returning current revision unchanged.",
                    run_id,
                    packet_record.packet_revision,
                    exc,
                )
                return packet_record.packet_revision

            new_revision = packet_record.packet_revision + 1
            uow.evidence_packets.persist_evidence_packet(
                new_packet.run_id,
                new_packet.research_spec_id,
                new_packet.coverage_revision,
                new_revision,
                _to_dict(new_packet),
            )
            return new_revision


__all__ = ["EvidenceService"]
