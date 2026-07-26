"""Agent-led handoff builder (Phase 7, issue #62).

This module provides ``HandoffBuilder``, a service that constructs a bounded,
self-contained ``HandoffPayload`` for a research run.  The payload gives a
host agent everything needed to draft a report without scanning scratch files
or triggering redundant semantic calls.

Key invariants:

* The payload is **read-only** — it never mutates database state.
* All citations resolve to passages inside the evidence packet.
* Limitations and unresolved items are explicit.
* No redundant inner-model semantic calls are triggered.
* Token limits are included when a budget policy is available.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from research_domain.models import HandoffPayload

from research_store.packet_validator import bounded_citation_ready_output

logger = logging.getLogger(__name__)


class HandoffBuilder:
    """Construct a bounded ``HandoffPayload`` from database state.

    Attributes:
        uow_factory: Callable that returns a ``PostgresUnitOfWork`` context.
        token_limits: Optional dict of effective token limits.  When
            ``None``, the payload's ``token_limits`` field will be ``None``.
        max_passages: Maximum number of passages in the citation-ready
            output.  Defaults to ``128``.
        max_claims: Maximum number of claims in the citation-ready output.
            Defaults to ``64``.
    """

    def __init__(
        self,
        uow_factory,
        *,
        token_limits: dict[str, int] | None = None,
        max_passages: int = 128,
        max_claims: int = 64,
    ):
        self.uow_factory = uow_factory
        self.token_limits = token_limits
        self.max_passages = max_passages
        self.max_claims = max_claims

    def build(self, run_id: UUID) -> HandoffPayload:
        """Construct a handoff payload for *run_id*.

        Args:
            run_id: The research run to hand off.

        Returns:
            A fully populated ``HandoffPayload``.

        Raises:
            ValueError: When the evidence packet or required data is missing.
        """
        with self.uow_factory() as uow:
            # 1. Load the latest evidence packet.
            packet_rec = uow.get_evidence_packet(run_id)
            if packet_rec is None:
                raise ValueError(
                    f"EvidencePacket not found for run {run_id}; "
                    "handoff requires a completed evidence packet."
                )

            packet_dict = packet_rec.to_dict()
            packet_payload = packet_dict["payload"]

            # 2. Load the research spec.
            spec_rec = uow.get_research_spec(run_id)
            if spec_rec is None:
                raise ValueError(
                    f"ResearchSpec not found for run {run_id}; "
                    "handoff requires a validated spec."
                )
            spec_payload = spec_rec.get("payload")

            # 3. Load the coverage summary.
            coverage_summary = uow.get_coverage_summary(run_id)
            if coverage_summary is None:
                # Rebuild the coverage summary from events.
                coverage_summary = self._rebuild_coverage_summary(uow, run_id)

            # 4. Extract limitations and unresolved items from the packet.
            limitations = tuple(packet_payload.get("limitations", []))
            unresolved_items = tuple(
                UUID(uid) if isinstance(uid, str) else uid
                for uid in packet_payload.get("unresolved_items", [])
            )

            # 5. Build the bounded citation-ready output.
            #    bounded_citation_ready_output expects an EvidencePacket object,
            #    so we load the dict first.
            citation_ready = self._build_citation_ready(packet_payload)

            # 6. Build an optional outline from the packet structure.
            outline = self._build_outline(packet_payload)

            # 7. Assemble the payload.
            payload = HandoffPayload(
                schema_version=HandoffPayload.SCHEMA_VERSION,
                run_id=run_id,
                research_spec=spec_payload,
                coverage_ledger=coverage_summary,
                evidence_packet=packet_payload,
                evidence_packet_revision=packet_rec.packet_revision,
                coverage_revision=packet_payload.get(
                    "coverage_revision", coverage_summary.get("coverage_revision", 1)
                ),
                limitations=limitations,
                unresolved_items=unresolved_items,
                outline=outline,
                citation_ready=citation_ready,
                token_limits=self.token_limits,
                created_at=datetime.now(timezone.utc),
            )

        return payload

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_citation_ready(self, packet_payload: dict[str, Any]) -> dict[str, Any]:
        """Build bounded, citation-ready output from a packet dict.

        Loads the packet dict as an ``EvidencePacket`` object and delegates
        to ``bounded_citation_ready_output`` to keep the logic DRY.
        """
        from research_domain.registry import load_model

        packet = load_model(packet_payload)
        return bounded_citation_ready_output(
            packet,
            max_passages=self.max_passages,
            max_claims=self.max_claims,
        )

    @staticmethod
    def _build_outline(packet_payload: dict[str, Any]) -> tuple[str, ...] | None:
        """Derive a minimal outline from the packet structure.

        Returns an ordered tuple of section headings, or ``None`` when the
        packet has no claims to structure an outline around.
        """
        claims = packet_payload.get("claims", [])
        if not claims:
            return None

        sections: list[str] = []

        # Always include an evidence summary section.
        sections.append("1. Evidence summary")

        # Group claims by semantic status for structured outline.
        supported = []
        contradicted = []
        qualified = []
        unsupported = []

        for claim in claims:
            status = claim.get("semantic_status", "")
            statement = claim.get("statement", "Untitled claim")
            if status == "supported":
                supported.append(statement)
            elif status == "contradicted":
                contradicted.append(statement)
            elif status == "qualified":
                qualified.append(statement)
            else:
                unsupported.append(statement)

        if supported:
            sections.append(
                f"2. Supported findings ({len(supported)} claim{'s' if len(supported) != 1 else ''})"
            )
        if contradicted:
            sections.append(
                f"3. Contradicted claims ({len(contradicted)} claim{'s' if len(contradicted) != 1 else ''})"
            )
        if qualified:
            sections.append(
                f"4. Qualified findings ({len(qualified)} claim{'s' if len(qualified) != 1 else ''})"
            )
        if unsupported:
            sections.append(
                f"5. Unsupported claims ({len(unsupported)} claim{'s' if len(unsupported) != 1 else ''})"
            )

        # Always end with limitations and unresolved items.
        sections.append("6. Limitations and unresolved items")

        return tuple(sections)

    @staticmethod
    def _rebuild_coverage_summary(uow, run_id: UUID) -> dict[str, Any]:
        """Rebuild the coverage summary from coverage events."""
        revision = uow.coverage.get_current_revision(run_id)
        if revision < 1:
            return {
                "schema_version": "coverage-ledger-v1",
                "run_id": str(run_id),
                "coverage_revision": 0,
                "total_items": 0,
                "status_counts": {},
                "type_counts": {},
                "overall_status": "unassessed",
            }
        # Rebuild projection from events.
        events = uow.coverage.list_coverage_events(run_id, limit=10000, offset=0)
        items_by_id: dict[UUID, dict[str, Any]] = {}
        for event in events:
            item_id = UUID(event["coverage_item_id"])
            if item_id not in items_by_id:
                items_by_id[item_id] = {
                    "coverage_item_id": str(item_id),
                    "item_type": event["item_type"],
                    "status": event["status"],
                    "freshness_status": event.get("freshness_status", "unknown"),
                }
            # Last event wins for mutable fields.
            items_by_id[item_id]["status"] = event["status"]
            if "freshness_status" in event:
                items_by_id[item_id]["freshness_status"] = event["freshness_status"]

        status_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        for item in items_by_id.values():
            status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
            type_counts[item["item_type"]] = type_counts.get(item["item_type"], 0) + 1

        overall_status = "unassessed"
        if items_by_id:
            # Determine overall status from the most common status.
            overall_status = max(status_counts, key=status_counts.get)

        return {
            "schema_version": "coverage-ledger-v1",
            "run_id": str(run_id),
            "coverage_revision": revision,
            "total_items": len(items_by_id),
            "status_counts": dict(sorted(status_counts.items())),
            "type_counts": dict(sorted(type_counts.items())),
            "overall_status": overall_status,
        }
