"""Provider-safe activation of persisted SearchPlan temporal windows."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .recency import normalize_recency_window
from .temporal_policy import parse_bound


class TemporalPlanTransportError(ValueError):
    """A bounded plan cannot be represented as a non-narrowing provider filter."""


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
    """Derive the exact local qdr request needed to cover a plan TimeWindow.

    Firecrawl exposes relative recency filters, not arbitrary historical ranges.
    The provider request therefore covers the interval from the persisted
    earliest publication bound through the evaluation clock; the existing
    PostgreSQL-backed temporal policy still enforces the exact start/end and
    rejects missing/future publication authority locally.
    """

    requirement = query.get("freshness_requirement")
    if not isinstance(requirement, Mapping):
        return None
    start_raw = requirement.get("start")
    end_raw = requirement.get("end")
    if not start_raw and not end_raw:
        return None
    if not start_raw:
        raise TemporalPlanTransportError(
            "bounded search plan has an end bound but no start bound; provider "
            "discovery cannot remain bounded without narrowing the authoritative "
            "window"
        )

    reference = _clock(evaluated_at)
    start = parse_bound(str(start_raw))
    seconds = max(1.0, (reference - start).total_seconds())
    days = max(1, math.ceil(seconds / 86400))
    requested = f"qdr:{days}d"
    normalized = normalize_recency_window(requested)
    if normalized is None or normalized.provider_tbs is None:
        raise TemporalPlanTransportError(
            "bounded search plan exceeds the provider's documented non-narrowing "
            "recency range; unbounded provider discovery is not permitted"
        )
    return requested


__all__ = ["TemporalPlanTransportError", "plan_query_recency_tbs"]
