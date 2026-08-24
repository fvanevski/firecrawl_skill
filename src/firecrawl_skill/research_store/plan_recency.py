"""Provider-safe activation of persisted SearchPlan discovery windows."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .recency import normalize_recency_window
from .temporal_policy import parse_bound


class TemporalPlanTransportError(ValueError):
    """A malformed discovery window cannot be represented safely."""


def _clock(value: datetime | None) -> datetime:
    resolved = value or datetime.now(timezone.utc)
    if resolved.tzinfo is None:
        raise ValueError("temporal plan evaluation clock must be timezone-aware")
    return resolved.astimezone(timezone.utc)


def plan_query_recency_tbs(
    query: Mapping[str, Any],
    *,
    evaluated_at: datetime | None = None,
) -> str | None:
    """Derive exact local qdr recency for a non-authoritative discovery window.

    Firecrawl only exposes coarse relative recency filters.  The returned local
    qdr request is therefore used when it maps to a provider filter that is a
    non-narrowing superset.  If the provider cannot safely bound the discovery
    interval, ``None`` deliberately requests unbounded provider discovery; the
    persisted ResearchSpec and local temporal policy remain the evidence
    authority.
    """

    requirement = query.get("freshness_requirement")
    if not isinstance(requirement, Mapping):
        return None
    start_raw = requirement.get("start")
    end_raw = requirement.get("end")
    if not start_raw and not end_raw:
        return None
    if not start_raw:
        return None

    reference = _clock(evaluated_at)
    try:
        start = parse_bound(str(start_raw))
    except (TypeError, ValueError) as exc:
        raise TemporalPlanTransportError("discovery start is not a valid temporal bound") from exc
    if start > reference:
        raise TemporalPlanTransportError(
            "bounded discovery starts in the future and cannot be represented by past recency"
        )
    seconds = max(1.0, (reference - start).total_seconds())
    days = max(1, math.ceil(seconds / 86400))
    requested = f"qdr:{days}d"
    normalized = normalize_recency_window(requested)
    if normalized is None or normalized.provider_tbs is None:
        return None
    return requested


__all__ = ["TemporalPlanTransportError", "plan_query_recency_tbs"]
