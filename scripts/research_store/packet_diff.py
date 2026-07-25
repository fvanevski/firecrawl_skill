"""EvidencePacket diff tool.

Computes the differences between two evidence packet revisions and
identifies changes in evidence (passages) and coverage (claims, groups,
unresolved items).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from research_domain.models import (
    EvidenceGroup,
    EvidencePacket,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PassageDelta:
    """Difference for a single passage."""

    passage_id: str
    delta: str  # "added", "removed", "modified"
    old_url: str | None = None
    new_url: str | None = None
    old_text_len: int | None = None
    new_text_len: int | None = None


@dataclass(frozen=True)
class ClaimDelta:
    """Difference for a single claim."""

    claim_id: str
    delta: str  # "added", "removed", "modified"
    old_statement: str | None = None
    new_statement: str | None = None
    old_status: str | None = None
    new_status: str | None = None


@dataclass(frozen=True)
class GroupDelta:
    """Difference for an evidence group."""

    group_id: str
    delta: str  # "added", "removed", "modified"
    old_passage_ids: list[str] | None = None
    new_passage_ids: list[str] | None = None
    old_rationale: str | None = None
    new_rationale: str | None = None


@dataclass(frozen=True)
class PacketDiff:
    """Difference between two EvidencePacket revisions.

    Attributes:
        old_run_id: Run ID of the old packet.
        new_run_id: Run ID of the new packet.
        old_revision: Revision number of the old packet.
        new_revision: Revision number of the new packet.
        added_passages: Passages present in new but not in old.
        removed_passages: Passages present in old but not in new.
        modified_passages: Passages present in both but with differences.
        added_claims: Claims present in new but not in old.
        removed_claims: Claims present in old but not in new.
        modified_claims: Claims present in both but with differences.
        added_groups: Groups present in new but not in old.
        removed_groups: Groups present in old but not in new.
        modified_groups: Groups present in both but with differences.
        added_unresolved: Unresolved items present in new but not in old.
        removed_unresolved: Unresolved items present in old but not in new.
        added_omitted_passages: Omitted passages present in new but not in old.
        removed_omitted_passages: Omitted passages present in old but not in new.
        modified_omitted_passages: Omitted passages present in both but
            with differences.
        coverage_revision_changed: Whether coverage revision changed.
        token_count_changed: Whether total token count changed.
        summary: Human-readable summary.
    """

    old_run_id: str
    new_run_id: str
    old_revision: int
    new_revision: int
    added_passages: tuple[PassageDelta, ...] = ()
    removed_passages: tuple[PassageDelta, ...] = ()
    modified_passages: tuple[PassageDelta, ...] = ()
    added_omitted_passages: tuple[PassageDelta, ...] = ()
    removed_omitted_passages: tuple[PassageDelta, ...] = ()
    modified_omitted_passages: tuple[PassageDelta, ...] = ()
    added_claims: tuple[ClaimDelta, ...] = ()
    removed_claims: tuple[ClaimDelta, ...] = ()
    modified_claims: tuple[ClaimDelta, ...] = ()
    added_groups: tuple[GroupDelta, ...] = ()
    removed_groups: tuple[GroupDelta, ...] = ()
    modified_groups: tuple[GroupDelta, ...] = ()
    added_unresolved: tuple[str, ...] = ()
    removed_unresolved: tuple[str, ...] = ()
    coverage_revision_changed: bool = False
    token_count_changed: bool = False

    @property
    def summary(self) -> str:
        parts = []
        if self.added_passages:
            parts.append(f"+{len(self.added_passages)} passages")
        if self.removed_passages:
            parts.append(f"-{len(self.removed_passages)} passages")
        if self.modified_passages:
            parts.append(f"~{len(self.modified_passages)} passages modified")
        if self.added_omitted_passages:
            parts.append(f"+{len(self.added_omitted_passages)} omitted passages")
        if self.removed_omitted_passages:
            parts.append(f"-{len(self.removed_omitted_passages)} omitted passages")
        if self.modified_omitted_passages:
            parts.append(
                f"~{len(self.modified_omitted_passages)} omitted passages modified"
            )
        if self.added_claims:
            parts.append(f"+{len(self.added_claims)} claims")
        if self.removed_claims:
            parts.append(f"-{len(self.removed_claims)} claims")
        if self.modified_claims:
            parts.append(f"~{len(self.modified_claims)} claims modified")
        if self.added_groups:
            parts.append(f"+{len(self.added_groups)} groups")
        if self.removed_groups:
            parts.append(f"-{len(self.removed_groups)} groups")
        if self.coverage_revision_changed:
            parts.append("coverage revision changed")
        if self.token_count_changed:
            parts.append("token count changed")
        if not parts:
            return "no differences"
        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_run_id": self.old_run_id,
            "new_run_id": self.new_run_id,
            "old_revision": self.old_revision,
            "new_revision": self.new_revision,
            "summary": self.summary,
            "added_passages": [
                {
                    "passage_id": p.passage_id,
                    "delta": p.delta,
                    "old_url": p.old_url,
                    "new_url": p.new_url,
                    "old_text_len": p.old_text_len,
                    "new_text_len": p.new_text_len,
                }
                for p in self.added_passages
            ],
            "removed_passages": [
                {
                    "passage_id": p.passage_id,
                    "delta": p.delta,
                    "old_url": p.old_url,
                    "new_url": p.new_url,
                    "old_text_len": p.old_text_len,
                    "new_text_len": p.new_text_len,
                }
                for p in self.removed_passages
            ],
            "modified_passages": [
                {
                    "passage_id": p.passage_id,
                    "delta": p.delta,
                    "old_url": p.old_url,
                    "new_url": p.new_url,
                    "old_text_len": p.old_text_len,
                    "new_text_len": p.new_text_len,
                }
                for p in self.modified_passages
            ],
            "added_omitted_passages": [
                {
                    "passage_id": p.passage_id,
                    "delta": p.delta,
                    "old_url": p.old_url,
                    "new_url": p.new_url,
                    "old_text_len": p.old_text_len,
                    "new_text_len": p.new_text_len,
                }
                for p in self.added_omitted_passages
            ],
            "removed_omitted_passages": [
                {
                    "passage_id": p.passage_id,
                    "delta": p.delta,
                    "old_url": p.old_url,
                    "new_url": p.new_url,
                    "old_text_len": p.old_text_len,
                    "new_text_len": p.new_text_len,
                }
                for p in self.removed_omitted_passages
            ],
            "modified_omitted_passages": [
                {
                    "passage_id": p.passage_id,
                    "delta": p.delta,
                    "old_url": p.old_url,
                    "new_url": p.new_url,
                    "old_text_len": p.old_text_len,
                    "new_text_len": p.new_text_len,
                }
                for p in self.modified_omitted_passages
            ],
            "added_claims": [
                {
                    "claim_id": c.claim_id,
                    "delta": c.delta,
                    "old_statement": c.old_statement,
                    "new_statement": c.new_statement,
                    "old_status": c.old_status,
                    "new_status": c.new_status,
                }
                for c in self.added_claims
            ],
            "removed_claims": [
                {
                    "claim_id": c.claim_id,
                    "delta": c.delta,
                    "old_statement": c.old_statement,
                    "new_statement": c.new_statement,
                    "old_status": c.old_status,
                    "new_status": c.new_status,
                }
                for c in self.removed_claims
            ],
            "modified_claims": [
                {
                    "claim_id": c.claim_id,
                    "delta": c.delta,
                    "old_statement": c.old_statement,
                    "new_statement": c.new_statement,
                    "old_status": c.old_status,
                    "new_status": c.new_status,
                }
                for c in self.modified_claims
            ],
            "added_groups": [
                {
                    "group_id": g.group_id,
                    "delta": g.delta,
                    "old_passage_ids": g.old_passage_ids,
                    "new_passage_ids": g.new_passage_ids,
                    "old_rationale": g.old_rationale,
                    "new_rationale": g.new_rationale,
                }
                for g in self.added_groups
            ],
            "removed_groups": [
                {
                    "group_id": g.group_id,
                    "delta": g.delta,
                    "old_passage_ids": g.old_passage_ids,
                    "new_passage_ids": g.new_passage_ids,
                    "old_rationale": g.old_rationale,
                    "new_rationale": g.new_rationale,
                }
                for g in self.removed_groups
            ],
            "modified_groups": [
                {
                    "group_id": g.group_id,
                    "delta": g.delta,
                    "old_passage_ids": g.old_passage_ids,
                    "new_passage_ids": g.new_passage_ids,
                    "old_rationale": g.old_rationale,
                    "new_rationale": g.new_rationale,
                }
                for g in self.modified_groups
            ],
            "added_unresolved": list(self.added_unresolved),
            "removed_unresolved": list(self.removed_unresolved),
            "coverage_revision_changed": self.coverage_revision_changed,
            "token_count_changed": self.token_count_changed,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def diff_packets(
    old_packet: EvidencePacket,
    new_packet: EvidencePacket,
    *,
    old_revision: int | None = None,
    new_revision: int | None = None,
) -> PacketDiff:
    """Compute the difference between two EvidencePacket revisions.

    Args:
        old_packet: The older EvidencePacket.
        new_packet: The newer EvidencePacket.
        old_revision: Revision number of the old packet (for reporting).
        new_revision: Revision number of the new packet (for reporting).

    Returns:
        A ``PacketDiff`` describing all changes.
    """
    old_rev = old_revision or 0
    new_rev = new_revision or 0

    # Build lookup maps by ID.
    old_passages = {p.passage_id: p for p in old_packet.passages}
    new_passages = {p.passage_id: p for p in new_packet.passages}

    old_claims = {c.claim_id: c for c in old_packet.claims}
    new_claims = {c.claim_id: c for c in new_packet.claims}

    old_groups = {g.group_id: g for g in _all_groups(old_packet)}
    new_groups = {g.group_id: g for g in _all_groups(new_packet)}

    old_unresolved = set(old_packet.unresolved_items)
    new_unresolved = set(new_packet.unresolved_items)

    # Passage deltas.
    added_passages = []
    removed_passages = []
    modified_passages = []

    for pid, new_p in new_passages.items():
        if pid not in old_passages:
            added_passages.append(
                PassageDelta(
                    passage_id=str(pid),
                    delta="added",
                    new_url=new_p.source_url,
                    new_text_len=len(new_p.text),
                )
            )
        else:
            old_p = old_passages[pid]
            if (
                old_p.source_url != new_p.source_url
                or old_p.text != new_p.text
                or old_p.candidate_id != new_p.candidate_id
            ):
                modified_passages.append(
                    PassageDelta(
                        passage_id=str(pid),
                        delta="modified",
                        old_url=old_p.source_url,
                        new_url=new_p.source_url,
                        old_text_len=len(old_p.text),
                        new_text_len=len(new_p.text),
                    )
                )

    for pid, old_p in old_passages.items():
        if pid not in new_passages:
            removed_passages.append(
                PassageDelta(
                    passage_id=str(pid),
                    delta="removed",
                    old_url=old_p.source_url,
                    old_text_len=len(old_p.text),
                )
            )

    # Build lookup maps for omitted passages.
    old_omitted = {p.passage_id: p for p in old_packet.omitted_passages}
    new_omitted = {p.passage_id: p for p in new_packet.omitted_passages}

    # Omitted passage deltas.
    added_omitted = []
    removed_omitted = []
    modified_omitted = []

    for pid, new_p in new_omitted.items():
        if pid not in old_omitted:
            added_omitted.append(
                PassageDelta(
                    passage_id=str(pid),
                    delta="added",
                    new_url=new_p.source_url,
                    new_text_len=len(new_p.text),
                )
            )
        else:
            old_p = old_omitted[pid]
            if (
                old_p.source_url != new_p.source_url
                or old_p.text != new_p.text
                or old_p.candidate_id != new_p.candidate_id
            ):
                modified_omitted.append(
                    PassageDelta(
                        passage_id=str(pid),
                        delta="modified",
                        old_url=old_p.source_url,
                        new_url=new_p.source_url,
                        old_text_len=len(old_p.text),
                        new_text_len=len(new_p.text),
                    )
                )

    for pid, old_p in old_omitted.items():
        if pid not in new_omitted:
            removed_omitted.append(
                PassageDelta(
                    passage_id=str(pid),
                    delta="removed",
                    old_url=old_p.source_url,
                    old_text_len=len(old_p.text),
                )
            )

    # Claim deltas.
    added_claims = []
    removed_claims = []
    modified_claims = []

    for cid, new_c in new_claims.items():
        if cid not in old_claims:
            added_claims.append(
                ClaimDelta(
                    claim_id=str(cid),
                    delta="added",
                    new_statement=new_c.statement,
                    new_status=new_c.semantic_status.value,
                )
            )
        else:
            old_c = old_claims[cid]
            if (
                old_c.statement != new_c.statement
                or old_c.semantic_status != new_c.semantic_status
            ):
                modified_claims.append(
                    ClaimDelta(
                        claim_id=str(cid),
                        delta="modified",
                        old_statement=old_c.statement,
                        new_statement=new_c.statement,
                        old_status=old_c.semantic_status.value,
                        new_status=new_c.semantic_status.value,
                    )
                )

    for cid, old_c in old_claims.items():
        if cid not in new_claims:
            removed_claims.append(
                ClaimDelta(
                    claim_id=str(cid),
                    delta="removed",
                    old_statement=old_c.statement,
                    old_status=old_c.semantic_status.value,
                )
            )

    # Group deltas.
    added_groups = []
    removed_groups = []
    modified_groups = []

    for gid, new_g in new_groups.items():
        if gid not in old_groups:
            added_groups.append(
                GroupDelta(
                    group_id=str(gid),
                    delta="added",
                    new_passage_ids=[str(p) for p in new_g.passage_ids],
                    new_rationale=new_g.rationale,
                )
            )
        else:
            old_g = old_groups[gid]
            if (
                old_g.passage_ids != new_g.passage_ids
                or old_g.rationale != new_g.rationale
                or old_g.evaluated != new_g.evaluated
            ):
                modified_groups.append(
                    GroupDelta(
                        group_id=str(gid),
                        delta="modified",
                        old_passage_ids=[str(p) for p in old_g.passage_ids],
                        new_passage_ids=[str(p) for p in new_g.passage_ids],
                        old_rationale=old_g.rationale,
                        new_rationale=new_g.rationale,
                    )
                )

    for gid, old_g in old_groups.items():
        if gid not in new_groups:
            removed_groups.append(
                GroupDelta(
                    group_id=str(gid),
                    delta="removed",
                    old_passage_ids=[str(p) for p in old_g.passage_ids],
                    old_rationale=old_g.rationale,
                )
            )

    # Unresolved item deltas.
    added_unresolved = tuple(str(u) for u in sorted(new_unresolved - old_unresolved))
    removed_unresolved = tuple(str(u) for u in sorted(old_unresolved - new_unresolved))

    # Coverage revision change.
    coverage_revision_changed = (
        old_packet.coverage_revision != new_packet.coverage_revision
    )

    # Token count change (approximate via text length).
    old_token_count = sum(len(p.text) for p in old_packet.passages)
    new_token_count = sum(len(p.text) for p in new_packet.passages)
    token_count_changed = old_token_count != new_token_count

    return PacketDiff(
        old_run_id=str(old_packet.run_id),
        new_run_id=str(new_packet.run_id),
        old_revision=old_rev,
        new_revision=new_rev,
        added_passages=tuple(added_passages),
        removed_passages=tuple(removed_passages),
        modified_passages=tuple(modified_passages),
        added_omitted_passages=tuple(added_omitted),
        removed_omitted_passages=tuple(removed_omitted),
        modified_omitted_passages=tuple(modified_omitted),
        added_claims=tuple(added_claims),
        removed_claims=tuple(removed_claims),
        modified_claims=tuple(modified_claims),
        added_groups=tuple(added_groups),
        removed_groups=tuple(removed_groups),
        modified_groups=tuple(modified_groups),
        added_unresolved=added_unresolved,
        removed_unresolved=removed_unresolved,
        coverage_revision_changed=coverage_revision_changed,
        token_count_changed=token_count_changed,
    )


def _all_groups(packet: EvidencePacket) -> tuple[EvidenceGroup, ...]:
    """Return all groups from a packet as a single tuple."""
    return (
        packet.corroborating_groups
        + packet.contradicting_groups
        + packet.qualifying_groups
        + packet.near_duplicate_groups
    )
