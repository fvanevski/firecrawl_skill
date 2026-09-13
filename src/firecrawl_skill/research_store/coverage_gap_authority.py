"""Typed authority registry for persisted coverage gaps.

A coverage gap is an extensible workflow concept.  Its event names, durable
payload key, and public operator rationale live in one registry so new gap
kinds cannot accidentally be routed through a temporal-only implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class CoverageGapAuthority:
    kind: str
    gap_event: str
    resolved_event: str
    payload_key: str
    operator_reason: str


COVERAGE_GAP_AUTHORITIES: dict[str, CoverageGapAuthority] = {
    "temporal_coverage_gap": CoverageGapAuthority(
        kind="temporal_coverage_gap",
        gap_event="evidence.temporal_coverage_gap",
        resolved_event="evidence.temporal_coverage_resolved",
        payload_key="temporal_coverage_gap",
        operator_reason="authoritative temporal coverage remains unsatisfied",
    ),
    "exact_source_coverage_gap": CoverageGapAuthority(
        kind="exact_source_coverage_gap",
        gap_event="evidence.exact_source_coverage_gap",
        resolved_event="evidence.exact_source_coverage_resolved",
        payload_key="exact_source_coverage_gap",
        operator_reason="exact canonical-source authority remains unsatisfied",
    ),
}


class CoverageGapAuthorityError(ValueError):
    """Persisted coverage-gap authority is unsupported or malformed."""


def coverage_gap_authority(kind: object) -> CoverageGapAuthority:
    normalized = str(kind or "")
    contract = COVERAGE_GAP_AUTHORITIES.get(normalized)
    if contract is None:
        raise CoverageGapAuthorityError(
            f"unsupported typed coverage gap: {normalized!r}"
        )
    return contract


def active_coverage_gap(
    uow: Any,
    run_id: UUID,
    kind: object,
    *,
    max_events: int = 10_000,
    page_size: int = 100,
) -> dict[str, Any] | None:
    """Resolve one active typed gap from the immutable run-event journal."""

    if max_events < 1 or page_size < 1:
        raise CoverageGapAuthorityError("coverage-gap scan bounds must be positive")
    contract = coverage_gap_authority(kind)
    latest_gap: dict[str, Any] | None = None
    latest_gap_sequence = -1
    latest_resolution_sequence = -1
    offset = 0
    while offset < max_events:
        limit = min(page_size, max_events - offset)
        events = uow.runs.list_events(run_id, limit=limit, offset=offset)
        for event in events:
            sequence = int(event.get("sequence_number") or 0)
            event_type = str(event.get("event_type") or "")
            if event_type == contract.gap_event:
                payload = event.get("payload") or {}
                if not isinstance(payload, Mapping):
                    raise CoverageGapAuthorityError(
                        f"persisted {contract.kind} payload is malformed"
                    )
                gap = payload.get(contract.payload_key)
                if not isinstance(gap, Mapping) or gap.get("kind") != contract.kind:
                    raise CoverageGapAuthorityError(
                        f"persisted {contract.kind} authority is malformed"
                    )
                if sequence > latest_gap_sequence:
                    latest_gap = dict(gap)
                    latest_gap_sequence = sequence
            elif event_type == contract.resolved_event:
                latest_resolution_sequence = max(latest_resolution_sequence, sequence)
        offset += len(events)
        if len(events) < limit:
            break
    else:
        raise CoverageGapAuthorityError(
            f"run event history exceeds bounded {contract.kind} authority scan"
        )

    if latest_gap is None or latest_resolution_sequence > latest_gap_sequence:
        return None
    return latest_gap


__all__ = [
    "COVERAGE_GAP_AUTHORITIES",
    "CoverageGapAuthority",
    "CoverageGapAuthorityError",
    "active_coverage_gap",
    "coverage_gap_authority",
]
