"""Pure temporal qualification policy shared by evidence and terminal gates."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_temporal(value: Any) -> datetime | None:
    """Normalize ISO/RFC temporal evidence without fabricating a timestamp."""
    if value is None or isinstance(value, (dict, list, tuple)):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(raw)
            except (TypeError, ValueError, IndexError):
                return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def parse_bound(value: str, *, end_of_day: bool = False) -> datetime:
    raw = str(value).strip()
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if end_of_day and _DATE_ONLY.fullmatch(raw):
        return parsed + timedelta(days=1)
    return parsed


def has_temporal_obligations(spec: Mapping[str, Any]) -> bool:
    window = spec.get("time_window") or {}
    if isinstance(window, Mapping) and (window.get("start") or window.get("end")):
        return True
    return any(
        item.get("max_age_days") is not None
        for item in spec.get("freshness_requirements", ())
        if isinstance(item, Mapping)
    )


def publication_in_window(
    published_at: Any,
    time_window: Mapping[str, Any] | None,
) -> bool:
    window = time_window or {}
    start_raw = window.get("start")
    end_raw = window.get("end")
    if not start_raw and not end_raw:
        return True
    publication = normalize_temporal(published_at)
    if publication is None:
        return False
    start = parse_bound(str(start_raw)) if start_raw else None
    end = parse_bound(str(end_raw), end_of_day=True) if end_raw else None
    return (start is None or publication >= start) and (
        end is None or publication < end
    )


def freshness_satisfied(
    *,
    published_at: Any,
    updated_at: Any,
    max_age_days: int,
    now: datetime | None = None,
) -> bool:
    reference = now or datetime.now(timezone.utc)
    cutoff = reference - timedelta(days=int(max_age_days))
    values = (
        normalize_temporal(published_at),
        normalize_temporal(updated_at),
    )
    return any(value is not None and value >= cutoff for value in values)


def passage_temporally_qualifies(
    passage: Mapping[str, Any],
    spec: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Whether one passage may satisfy bounded semantic coverage.

    Retrieval time is deliberately ignored.  An explicit publication window
    uses publication only.  Numeric freshness requirements may use publication
    or an explicit update/modification timestamp.
    """
    if not publication_in_window(passage.get("published_at"), spec.get("time_window")):
        return False
    for requirement in spec.get("freshness_requirements", ()):
        if not isinstance(requirement, Mapping):
            continue
        max_age = requirement.get("max_age_days")
        if max_age is None:
            continue
        if not freshness_satisfied(
            published_at=passage.get("published_at"),
            updated_at=passage.get("updated_at") or passage.get("last_modified"),
            max_age_days=int(max_age),
            now=now,
        ):
            return False
    return True


__all__ = [
    "freshness_satisfied",
    "has_temporal_obligations",
    "normalize_temporal",
    "parse_bound",
    "passage_temporally_qualifies",
    "publication_in_window",
]
