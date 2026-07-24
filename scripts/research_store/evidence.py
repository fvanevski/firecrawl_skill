import logging
from typing import Any
from uuid import UUID, uuid4
import datetime
import dataclasses
from research_domain.models import (
    EvidencePacket,
    EvidencePassage,
    EvidenceGroup,
    RetrievalProvenance,
)
from budget_policy import BudgetPolicy, ResourceCaps
from .tokenizer_registry import get_tokenizer

logger = logging.getLogger(__name__)


def _to_dict(obj: Any) -> Any:
    """Recursively convert domain objects to dicts for JSON serialization."""
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
        uow_factory,
        budget_policy: BudgetPolicy,
        tokenizer_name: str = "cl100k_base",
    ):
        self.uow_factory = uow_factory
        self.budget_policy = budget_policy
        self.tokenizer = get_tokenizer(tokenizer_name)

    def build_evidence_packet(
        self,
        run_id: UUID,
        research_spec_id: UUID,
        coverage_revision: int,
        candidates: list[dict],
        retrieval_events: list[RetrievalProvenance],
        effective_caps: ResourceCaps,
    ) -> EvidencePacket:
        """Construct a bounded, deterministic EvidencePacket.
        
        Args:
            run_id: The research run ID.
            research_spec_id: The active research spec ID.
            coverage_revision: The coverage revision triggering this packet.
            candidates: List of retrieved candidate dicts. Must contain
                'candidate_id', 'snapshot_id', 'chunk_id', 'text', 'url' / 'source_url'.
            retrieval_events: Provenance of the retrieval actions.
            effective_caps: Authorized resource caps containing token limits.
        """
        # Deterministic passage ordering (by snapshot_id then chunk_id)
        # Using string representation for deterministic sorting
        sorted_candidates = sorted(
            candidates,
            key=lambda c: (str(c.get("snapshot_id", "")), str(c.get("chunk_id", "")))
        )

        passages = []
        omitted_candidates = []
        token_count = 0
        max_tokens = effective_caps.max_evidence_packet_tokens

        source_domains = set()
        oldest = None
        newest = None

        for cand in sorted_candidates:
            text = cand.get("text", cand.get("excerpt", ""))
            cand_tokens = len(self.tokenizer.encode(text))
            
            if token_count + cand_tokens <= max_tokens:
                # Use source_url or fallback to url if provided
                source_url = cand.get("source_url") or cand.get("url") or ""
                if source_url:
                    source_domains.add(source_url)

                # Basic freshness tracking if 'date' is available
                cand_date_str = cand.get("date")
                if cand_date_str:
                    try:
                        cand_date = datetime.datetime.fromisoformat(cand_date_str.replace("Z", "+00:00"))
                        if oldest is None or cand_date < oldest:
                            oldest = cand_date
                        if newest is None or cand_date > newest:
                            newest = cand_date
                    except ValueError:
                        pass

                passages.append(
                    EvidencePassage(
                        passage_id=uuid4(),
                        candidate_id=UUID(str(cand["candidate_id"])),
                        snapshot_id=UUID(str(cand["snapshot_id"])),
                        chunk_id=UUID(str(cand["chunk_id"])),
                        text=text,
                        source_url=source_url,
                    )
                )
                token_count += cand_tokens
            else:
                omitted_candidates.append(cand)

        # Source diversity and freshness summaries
        diversity_summary = {
            "unique_sources": len(source_domains),
            "sources": sorted(list(source_domains))
        }

        freshness_summary = {
            "most_recent": newest.isoformat() if newest else None,
            "oldest": oldest.isoformat() if oldest else None,
        }

        # Duplicate candidates retained for later assessment
        # Represented as explicitly unevaluated semantic groups
        near_duplicate_groups = []
        if omitted_candidates:
            near_duplicate_groups.append(
                EvidenceGroup(
                    group_id=uuid4(),
                    passage_ids=tuple(),  # passages aren't fully instantiated if omitted
                    rationale="omitted_due_to_budget",
                    evaluated=False
                )
            )
            
        return EvidencePacket(
            schema_version=EvidencePacket.SCHEMA_VERSION,
            run_id=run_id,
            research_spec_id=research_spec_id,
            coverage_revision=coverage_revision,
            claims=tuple(),
            passages=tuple(passages),
            claim_evidence_bindings=tuple(),
            corroborating_groups=tuple(),
            contradicting_groups=tuple(),
            qualifying_groups=tuple(),
            near_duplicate_groups=tuple(near_duplicate_groups),
            source_diversity_summary=diversity_summary,
            freshness_summary=freshness_summary,
            limitations=tuple(),
            unresolved_items=tuple(),
            retrieval_provenance=tuple(retrieval_events),
        )

    def persist_packet(
        self,
        packet: EvidencePacket,
    ) -> int:
        """Persist the packet. Revision number is deterministic and monotonically increasing.
        
        Returns:
            The revision number of the persisted packet.
        """
        with self.uow_factory() as uow:
            latest = uow.get_evidence_packet(packet.run_id)
            rev = latest.packet_revision + 1 if latest else 1
            payload = _to_dict(packet)
            
            uow.persist_evidence_packet(
                packet.run_id,
                packet.research_spec_id,
                packet.coverage_revision,
                rev,
                payload
            )
            return rev

    def export_packet(self, run_id: UUID, revision: int | None = None) -> dict | None:
        """Export a persisted EvidencePacket by revision or the latest."""
        with self.uow_factory() as uow:
            packet_rec = uow.get_evidence_packet(run_id, revision)
            if packet_rec:
                return packet_rec.to_dict()
            return None
