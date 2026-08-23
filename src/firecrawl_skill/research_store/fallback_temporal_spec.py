"""Deterministic temporal materialization for smart-search fallback specs."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from firecrawl_skill.research_domain.models import (
    ExecutionMode,
    FreshnessRequirement,
    ResearchSpec,
    TimeWindow,
)

from .budget_policy import conservative_research_spec

_ISO_DATE = r"\d{4}-\d{2}-\d{2}"
_ISO_RANGE = re.compile(
    rf"(?:\bfrom\s+)?(?P<start>{_ISO_DATE})\s+"
    rf"(?:through|to|until|–|—)\s+(?P<end>{_ISO_DATE})\b",
    re.IGNORECASE,
)
_PAST_DAYS = re.compile(r"\bpast\s+(?P<count>[1-9]\d*)\s+days?\b", re.IGNORECASE)
_TEMPORAL_SIGNAL = re.compile(
    rf"{_ISO_DATE}|\b(?:past|since|before|after|between|as\s+of|today|yesterday|"
    r"latest|recent|currently|current|last\s+(?:day|week|month|year))\b",
    re.IGNORECASE,
)


class FallbackTemporalError(ValueError):
    """The objective is temporal but cannot be encoded deterministically."""


def _evaluation_clock(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("fallback temporal evaluation clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_date(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _freshness_id(spec: ResearchSpec, description: str):
    namespace = uuid5(NAMESPACE_URL, str(spec.research_spec_id))
    return uuid5(namespace, f"fallback-temporal\0{description}")


def materialize_smart_fallback_spec(
    objective: str,
    *,
    execution_mode: ExecutionMode | str,
    evaluated_at: datetime,
    research_archetype: str = "general",
) -> ResearchSpec:
    """Build the FR-003 fallback without discarding explicit temporal semantics.

    Supported temporal forms are deliberately narrow. If an objective contains
    an evident temporal constraint that is outside this grammar, callers must
    require an explicit ``--research-spec`` instead of persisting an unbounded
    fallback.
    """

    clock = _evaluation_clock(evaluated_at)
    base = conservative_research_spec(objective, research_archetype)
    mode = (
        execution_mode
        if isinstance(execution_mode, ExecutionMode)
        else ExecutionMode(str(execution_mode))
    )

    range_matches = list(_ISO_RANGE.finditer(objective))
    past_matches = list(_PAST_DAYS.finditer(objective))
    supported_count = len(range_matches) + len(past_matches)
    if supported_count > 1:
        raise FallbackTemporalError(
            "objective contains multiple temporal constraints; supply --research-spec "
            "with one authoritative TimeWindow"
        )

    if range_matches:
        match = range_matches[0]
        start_raw = match.group("start")
        end_raw = match.group("end")
        start_date = _parse_date(start_raw)
        if start_date > _parse_date(end_raw):
            raise FallbackTemporalError(
                "objective temporal range starts after it ends; supply --research-spec"
            )
        if (clock - start_date).total_seconds() > 366 * 86400:
            raise FallbackTemporalError(
                "objective temporal range exceeds the provider's bounded recency range; "
                "supply --research-spec"
            )
        description = f"objective interval {start_raw} through {end_raw}"
        window = TimeWindow(start_raw, end_raw, description, "none")
        freshness = (
            FreshnessRequirement(
                _freshness_id(base, description),
                "Required evidence publication must fall within the explicit objective interval.",
                None,
            ),
        )
    elif past_matches:
        count = int(past_matches[0].group("count"))
        if count > 366:
            raise FallbackTemporalError(
                "past-N-days fallback exceeds the provider's bounded recency range; "
                "supply --research-spec"
            )
        start = clock - timedelta(days=count)
        description = f"past {count} days resolved at {clock.isoformat()}"
        window = TimeWindow(
            start.isoformat(),
            clock.isoformat(),
            description,
            "none",
        )
        freshness = (
            FreshnessRequirement(
                _freshness_id(base, description),
                f"Required fresh evidence must be no older than {count} days.",
                count,
            ),
        )
    else:
        if _TEMPORAL_SIGNAL.search(objective):
            raise FallbackTemporalError(
                "objective expresses a temporal constraint that the deterministic "
                "fallback cannot encode unambiguously; supply --research-spec"
            )
        window = base.time_window
        freshness = base.freshness_requirements

    return replace(
        base,
        execution_mode=mode,
        time_window=window,
        freshness_requirements=freshness,
    )


__all__ = ["FallbackTemporalError", "materialize_smart_fallback_spec"]
