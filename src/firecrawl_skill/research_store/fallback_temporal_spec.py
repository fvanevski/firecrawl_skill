"""Narrow deterministic temporal materialization for degraded smart-search fallback."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone
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
_REDUNDANT_FRESHNESS = re.compile(r"\b(?:latest|recent|currently)\b", re.IGNORECASE)
_MONTH_NAME = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)"
)
_MONTH_COMPACT_RANGE = re.compile(
    rf"\b(?P<month>{_MONTH_NAME})\s+(?P<start_day>\d{{1,2}})(?:st|nd|rd|th)?\s*"
    rf"(?:-|–|—|to|through|until)\s*(?P<end_day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:,\s*|\s+)(?P<year>\d{{4}})\b",
    re.IGNORECASE,
)
_WEEKDAY = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
_TEMPORAL_UNIT = r"(?:hours?|days?|weeks?|months?|years?)"
_CLEAR_TEMPORAL_SIGNAL = re.compile(
    rf"{_ISO_DATE}"
    rf"|\b(?:past|last|next)\s+(?:[1-9]\d*\s+)?{_TEMPORAL_UNIT}\b"
    rf"|\b(?:last|next|this|current)\s+(?:{_WEEKDAY}|week|month|year)\b"
    rf"|\b(?:last|next)\s+{_WEEKDAY}\b"
    rf"|\b(?:today|yesterday|tomorrow|latest|recent|currently)\b"
    rf"|\b{_MONTH_NAME}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*\d{{4}})?\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH_NAME}(?:\s+\d{{4}})?\b"
    rf"|\b(?:during|throughout|in)\s+{_MONTH_NAME}(?:\s+\d{{4}})?\b"
    rf"|\b(?:during|throughout|since|before|after|as\s+of)\s+\d{{4}}\b"
    rf"|\b(?:since|before|after|as\s+of)\s+(?:{_WEEKDAY}|now)\b",
    re.IGNORECASE,
)
_MONTH_NUMBERS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_TEMPORAL_GUIDANCE = (
    "supply --research-spec validated by schemas/research-workflow/research-spec-v1.json "
    "or use the normal semantic smart-objective interpreter"
)


class FallbackTemporalError(ValueError):
    """The degraded grammar cannot encode the objective without guessing."""


def _evaluation_clock(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("fallback temporal evaluation clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_date(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _named_date(month: str, day: str, year: str) -> datetime:
    month_number = _MONTH_NUMBERS[month[:3].casefold()]
    try:
        return datetime(int(year), month_number, int(day), tzinfo=timezone.utc)
    except ValueError as exc:
        raise FallbackTemporalError(
            f"objective contains an impossible named date; {_TEMPORAL_GUIDANCE}"
        ) from exc


def _named_range(match: re.Match[str]) -> tuple[str, str]:
    start_month = end_month = match.group("month")
    year = match.group("year")
    start = _named_date(start_month, match.group("start_day"), year)
    end = _named_date(end_month, match.group("end_day"), year)
    if start > end:
        raise FallbackTemporalError(
            f"objective temporal range starts after it ends; {_TEMPORAL_GUIDANCE}"
        )
    return start.date().isoformat(), end.date().isoformat()


def _freshness_id(spec: ResearchSpec, description: str):
    namespace = uuid5(NAMESPACE_URL, str(spec.research_spec_id))
    return uuid5(namespace, f"fallback-temporal\0{description}")


def _unsupported_temporal_residue(
    objective: str,
    matches: tuple[re.Match[str], ...],
    *,
    allow_redundant_freshness: bool,
) -> bool:
    """Detect temporal language not consumed by the deliberately narrow grammar."""

    if not matches:
        return _CLEAR_TEMPORAL_SIGNAL.search(objective) is not None
    characters = list(objective)
    for match in matches:
        characters[match.start() : match.end()] = " " * (match.end() - match.start())
    residue = "".join(characters)
    if allow_redundant_freshness:
        residue = _REDUNDANT_FRESHNESS.sub(" ", residue)
    return _CLEAR_TEMPORAL_SIGNAL.search(residue) is not None


def materialize_smart_fallback_spec(
    objective: str,
    *,
    execution_mode: ExecutionMode | str,
    evaluated_at: datetime,
    research_archetype: str = "general",
) -> ResearchSpec:
    """Build only the conservative degraded/debug temporal fallback."""

    # Fail closed on a naive evaluation clock even though the degraded
    # grammar does not consume the normalized clock value.
    _evaluation_clock(evaluated_at)
    base = conservative_research_spec(objective, research_archetype)
    mode = (
        execution_mode
        if isinstance(execution_mode, ExecutionMode)
        else ExecutionMode(str(execution_mode))
    )

    iso_matches = list(_ISO_RANGE.finditer(objective))
    compact_matches = list(_MONTH_COMPACT_RANGE.finditer(objective))
    past_matches = list(_PAST_DAYS.finditer(objective))
    supported_matches = tuple(iso_matches + compact_matches + past_matches)
    if len(supported_matches) > 1:
        raise FallbackTemporalError(
            f"objective contains multiple temporal constraints; {_TEMPORAL_GUIDANCE}"
        )
    if _unsupported_temporal_residue(
        objective,
        supported_matches,
        allow_redundant_freshness=bool(past_matches),
    ):
        raise FallbackTemporalError(
            "objective expresses temporal semantics outside the degraded grammar; "
            + _TEMPORAL_GUIDANCE
        )

    start_raw: str | None = None
    end_raw: str | None = None
    if iso_matches:
        match = iso_matches[0]
        start_raw = match.group("start")
        end_raw = match.group("end")
    elif compact_matches:
        start_raw, end_raw = _named_range(compact_matches[0])

    if start_raw is not None and end_raw is not None:
        start_date = _parse_date(start_raw)
        if start_date > _parse_date(end_raw):
            raise FallbackTemporalError(
                f"objective temporal range starts after it ends; {_TEMPORAL_GUIDANCE}"
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
        description = f"fresh evidence no older than {count} days"
        window = base.time_window
        freshness = (
            FreshnessRequirement(
                _freshness_id(base, description),
                description,
                count,
            ),
        )
    else:
        window = base.time_window
        freshness = base.freshness_requirements

    return replace(
        base,
        execution_mode=mode,
        time_window=window,
        freshness_requirements=freshness,
    )


__all__ = ["FallbackTemporalError", "materialize_smart_fallback_spec"]
