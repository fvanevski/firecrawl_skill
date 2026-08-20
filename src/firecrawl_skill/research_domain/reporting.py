"""Agent-facing reporting and handoff domain contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class HandoffPayload:
    """Bounded, self-contained handoff for a host agent.

    The payload gives the host agent everything needed to draft a report
    without scanning scratch files or triggering redundant semantic calls.
    Every citation resolves to a passage inside the evidence packet.

    Attributes:
        schema_version: Always ``"handoff-payload-v1"``.
        run_id: The research run this handoff belongs to.
        research_spec: Serialized ``ResearchSpec`` with all requirements.
        coverage_ledger: Serialized ``CoverageLedger`` with current status.
        evidence_packet: Serialized ``EvidencePacket`` with claims, passages,
            bindings, and groups.
        evidence_packet_revision: The packet revision this payload reflects.
        coverage_revision: The coverage revision the ledger reflects.
        limitations: Explicit limitations and degraded states.
        unresolved_items: Coverage-item IDs that remain unresolved.
        outline: Optional structured outline for the report (``None`` when
            no outline was produced).
        citation_ready: Bounded, citation-ready subset of the packet
            (claims, passages, and bindings) suitable for host-agent
            synthesis.
        token_limits: Effective token limits derived from the budget policy
            (``None`` when limits are not applicable).
        created_at: Timestamp when the payload was constructed.
    """

    schema_version: str
    run_id: UUID
    research_spec: dict[str, Any]
    coverage_ledger: dict[str, Any]
    evidence_packet: dict[str, Any]
    evidence_packet_revision: int
    coverage_revision: int
    limitations: tuple[str, ...]
    unresolved_items: tuple[UUID, ...]
    outline: tuple[str, ...] | None
    citation_ready: dict[str, Any]
    token_limits: dict[str, int] | None
    created_at: datetime

    SCHEMA_VERSION = "handoff-payload-v1"

    def __post_init__(self) -> None:
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        # evidence_packet_revision == 0 is allowed for degraded payloads
        # where no evidence packet exists.
        if self.evidence_packet_revision < 0:
            raise ValueError("evidence_packet_revision must be >= 0")
        if self.coverage_revision < 0:
            raise ValueError("coverage_revision must be >= 0")

    def to_dict(self) -> dict[str, Any]:
        """Serialize the payload to a JSON-compatible dictionary."""
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "run_id": str(self.run_id),
            "research_spec": self.research_spec,
            "coverage_ledger": self.coverage_ledger,
            "evidence_packet": self.evidence_packet,
            "evidence_packet_revision": self.evidence_packet_revision,
            "coverage_revision": self.coverage_revision,
            "limitations": list(self.limitations),
            "unresolved_items": [str(uid) for uid in self.unresolved_items],
            "outline": (list(self.outline) if self.outline is not None else None),
            "citation_ready": self.citation_ready,
            "token_limits": self.token_limits,
            "created_at": (
                self.created_at.isoformat()
                if hasattr(self.created_at, "isoformat")
                else str(self.created_at)
            ),
        }
        return result
