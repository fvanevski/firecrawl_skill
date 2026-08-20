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

from firecrawl_skill.research_domain.models import HandoffPayload
from firecrawl_skill.research_domain.registry import load_model
from firecrawl_skill.research_store.packet_validator import (
    bounded_citation_ready_output,
)

logger = logging.getLogger(__name__)


def _plural_label(count: int, singular: str, plural: str | None = None) -> str:
    """Return *singular* when *count* is 1, else *plural* (or *singular* + "s").

    Examples::

        >>> _plural_label(1, "claim")
        'claim'
        >>> _plural_label(2, "claim")
        'claims'
        >>> _plural_label(3, "finding", "findings")
        'findings'
    """
    if plural is None:
        plural = f"{singular}s"
    return singular if count == 1 else plural


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

        If the evidence packet or coverage summary is missing, the builder
        produces a **degraded** payload that explicitly signals the gap
        rather than failing outright.  This allows a host agent to see
        exactly what is unavailable and avoid silent assumptions.

        Args:
            run_id: The research run to hand off.

        Returns:
            A fully populated ``HandoffPayload``.  When the evidence packet
            or coverage summary is missing the payload carries degradation
            notes in its ``limitations`` tuple.
        """
        with self.uow_factory() as uow:
            # 1. Load the latest evidence packet (may be None → degraded).
            packet_rec = uow.get_evidence_packet(run_id)
            packet_payload: dict[str, Any] | None = None
            evidence_packet_present = False
            evidence_packet_revision = 0

            if packet_rec is not None:
                packet_payload = packet_rec.to_dict()["payload"]
                evidence_packet_present = True
                evidence_packet_revision = packet_rec.packet_revision

            # 2. Load the research spec.
            spec_rec = uow.get_research_spec(run_id)
            spec_payload = spec_rec.get("payload") if spec_rec else {}
            spec_present = spec_rec is not None

            # 3. Load / rebuild the coverage summary.
            coverage_summary = uow.get_coverage_summary(run_id)
            coverage_degraded = False

            if coverage_summary is None:
                # Rebuild the coverage summary from events.
                coverage_summary, coverage_degraded = self._rebuild_coverage_summary(
                    uow, run_id
                )

            # 4. Extract limitations and unresolved items from the packet.
            limitations: list[str] = []
            unresolved_items: list[UUID] = []

            if packet_payload is not None:
                limitations.extend(packet_payload.get("limitations", []))
                unresolved_items.extend(
                    UUID(uid) if isinstance(uid, str) else uid
                    for uid in packet_payload.get("unresolved_items", [])
                )

            # 5. Add degradation notes to limitations when data is missing.
            if not evidence_packet_present:
                limitations.append(
                    "Evidence packet is missing; handoff is degraded — "
                    "no claims, passages, or citations are available."
                )
            if not spec_present:
                limitations.append(
                    "ResearchSpec is missing; handoff is degraded — "
                    "no validated requirements are available."
                )
            if coverage_degraded:
                limitations.append(
                    "Coverage summary was rebuilt from events (not from "
                    "snapshot); the summary may be incomplete if event "
                    "data was truncated."
                )

            # 6. Build the bounded citation-ready output.
            if packet_payload is not None:
                citation_ready = self._build_citation_ready(packet_payload)
            else:
                citation_ready = {
                    "claims": [],
                    "passages": [],
                    "bindings": {},
                    "groups": [],
                    "metadata": {
                        "schema_version": "evidence-packet-v1",
                        "run_id": str(run_id),
                        "degraded": True,
                        "reason": "evidence_packet_missing",
                    },
                }

            # 7. Build an optional outline from the packet structure.
            if packet_payload is not None:
                outline = self._build_outline(packet_payload)
            else:
                outline = None

            # 8. Assemble the payload.
            payload = HandoffPayload(
                schema_version=HandoffPayload.SCHEMA_VERSION,
                run_id=run_id,
                research_spec=spec_payload,
                coverage_ledger=coverage_summary,
                evidence_packet=packet_payload
                if packet_payload is not None
                else {
                    "schema_version": "evidence-packet-v1",
                    "run_id": str(run_id),
                    "degraded": True,
                    "reason": "evidence_packet_missing",
                },
                evidence_packet_revision=evidence_packet_revision,
                coverage_revision=coverage_summary.get("coverage_revision", 0),
                limitations=tuple(limitations),
                unresolved_items=tuple(unresolved_items),
                outline=outline,
                citation_ready=citation_ready,
                token_limits=self.token_limits,
                created_at=datetime.now(timezone.utc),
            )

        # coverage_revision defaults to 0 when coverage_summary is None and
        # _rebuild_coverage_summary returned revision 0 (no events).  The
        # .get() fallback is therefore a safety net for the rebuild path —
        # it never fires in practice but documents the degraded invariant.

        return payload

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_citation_ready(self, packet_payload: dict[str, Any]) -> dict[str, Any]:
        """Build bounded, citation-ready output from a packet dict.

        Loads the packet dict as an ``EvidencePacket`` object and delegates
        to ``bounded_citation_ready_output`` to keep the logic DRY.
        """
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
        counter = 1

        # Always include an evidence summary section.
        sections.append(f"{counter}. Evidence summary")
        counter += 1

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
            label = _plural_label(len(supported), "claim")
            sections.append(f"{counter}. Supported findings ({len(supported)} {label})")
            counter += 1
        if contradicted:
            label = _plural_label(len(contradicted), "claim")
            sections.append(
                f"{counter}. Contradicted claims ({len(contradicted)} {label})"
            )
            counter += 1
        if qualified:
            label = _plural_label(len(qualified), "claim")
            sections.append(f"{counter}. Qualified findings ({len(qualified)} {label})")
            counter += 1
        if unsupported:
            label = _plural_label(len(unsupported), "claim")
            sections.append(
                f"{counter}. Unsupported claims ({len(unsupported)} {label})"
            )
            counter += 1

        # Always end with limitations and unresolved items.
        sections.append(f"{counter}. Limitations and unresolved items")

        return tuple(sections)

    @staticmethod
    def _rebuild_coverage_summary(uow, run_id: UUID) -> tuple[dict[str, Any], bool]:
        """Rebuild the coverage summary from coverage events.

        Returns a ``(summary, is_degraded)`` tuple.  *is_degraded* is ``True``
        when the event list was truncated (more events than the fetch limit),
        meaning the summary may not match the authoritative snapshot.
        """
        revision = uow.coverage.get_current_revision(run_id)
        if revision < 1:
            return (
                {
                    "schema_version": "coverage-ledger-v1",
                    "run_id": str(run_id),
                    "coverage_revision": 0,
                    "total_items": 0,
                    "status_counts": {},
                    "type_counts": {},
                    "overall_status": "unassessed",
                },
                False,
            )

        # Fetch events with a generous limit.
        limit = 100_000
        events = uow.coverage.list_coverage_events(run_id, limit=limit, offset=0)

        # Detect truncation: if we got exactly ``limit`` rows there may be more.
        is_degraded = len(events) >= limit

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
            overall_status = max(status_counts, key=status_counts.get)

        summary = {
            "schema_version": "coverage-ledger-v1",
            "run_id": str(run_id),
            "coverage_revision": revision,
            "total_items": len(items_by_id),
            "status_counts": dict(sorted(status_counts.items())),
            "type_counts": dict(sorted(type_counts.items())),
            "overall_status": overall_status,
        }

        if is_degraded:
            # _degraded and _degradation_reason are internal implementation
            # markers (underscore-prefixed) — they are NOT part of the
            # coverage-ledger-v1 schema and should be ignored by consumers.
            summary["_degraded"] = True
            summary["_degradation_reason"] = (
                "event list truncated at 100000 rows; "
                "summary may not match the authoritative snapshot"
            )

        return (summary, is_degraded)
