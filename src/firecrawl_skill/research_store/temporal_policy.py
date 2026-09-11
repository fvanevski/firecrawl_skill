"""Pure temporal qualification policy shared by evidence and terminal gates."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BLOCKING_STATUSES = frozenset(
    {
        "explicit_invalid",
        "explicit_conflict",
        "explicit_provider_invalid",
        "explicit_provider_conflict",
    }
)
_TEMPORAL_BASES = frozenset(
    {
        "none",
        "publication_within",
        "publication_or_update_within",
        "event_within",
        "current_as_of",
        "conjunctive",
    }
)


@dataclass(frozen=True)
class TemporalQualification:
    """Obligation-specific temporal qualification for one passage."""

    status: str
    basis: str
    reason: str
    authoritative_time: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"satisfies", "violates", "unresolved"}:
            raise ValueError("unsupported temporal qualification status")
        if self.basis not in _TEMPORAL_BASES:
            raise ValueError("unsupported temporal qualification basis")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def resolved_temporal_basis(spec: Mapping[str, Any]) -> str:
    """Resolve explicit v1 ResearchSpec basis with a compatibility fallback."""

    raw_basis = spec.get("temporal_basis")
    if hasattr(raw_basis, "value"):
        raw_basis = raw_basis.value
    basis = str(raw_basis or "none")
    if basis != "none":
        if basis not in _TEMPORAL_BASES:
            raise ValueError(f"unsupported temporal basis: {basis}")
        return basis

    window = spec.get("time_window") or {}
    has_window = isinstance(window, Mapping) and bool(
        window.get("start") or window.get("end")
    )
    has_freshness = any(
        item.get("max_age_days") is not None
        for item in spec.get("freshness_requirements", ())
        if isinstance(item, Mapping)
    )
    if has_window and has_freshness:
        return "conjunctive"
    if has_window:
        return "publication_within"
    if has_freshness:
        return "publication_or_update_within"
    return "none"


def has_temporal_obligations(spec: Mapping[str, Any]) -> bool:
    return resolved_temporal_basis(spec) != "none"


def _reference(now: datetime | None) -> datetime:
    reference = now or datetime.now(timezone.utc)
    return reference if reference.tzinfo is not None else reference.replace(tzinfo=timezone.utc)


def _window_bounds(
    time_window: Mapping[str, Any] | None,
) -> tuple[datetime | None, datetime | None]:
    window = time_window or {}
    start_raw = window.get("start")
    end_raw = window.get("end")
    return (
        parse_bound(str(start_raw)) if start_raw else None,
        parse_bound(str(end_raw), end_of_day=True) if end_raw else None,
    )


def _in_window(
    value: datetime,
    time_window: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> bool:
    start, end = _window_bounds(time_window)
    reference = _reference(now)
    return (
        value <= reference
        and (start is None or value >= start)
        and (end is None or value < end)
    )


def publication_in_window(
    published_at: Any,
    time_window: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether authoritative publication time lies in the bounded interval."""

    start, end = _window_bounds(time_window)
    if start is None and end is None:
        return True
    publication = normalize_temporal(published_at)
    return publication is not None and _in_window(publication, time_window, now=now)


def freshness_satisfied(
    *,
    published_at: Any,
    updated_at: Any,
    max_age_days: int,
    now: datetime | None = None,
) -> bool:
    """Require fresh authority to be neither stale nor future-dated."""
    reference = _reference(now)
    cutoff = reference - timedelta(days=int(max_age_days))
    values = (
        normalize_temporal(published_at),
        normalize_temporal(updated_at),
    )
    return any(value is not None and cutoff <= value <= reference for value in values)


def _provenance(passage: Mapping[str, Any]) -> Mapping[str, Any]:
    value = passage.get("temporal_provenance")
    return value if isinstance(value, Mapping) else {}


def _field_status(
    passage: Mapping[str, Any], provenance: Mapping[str, Any], name: str
) -> str:
    value = provenance.get(f"{name}_status")
    if value is None:
        value = passage.get(f"{name}_status")
    return str(value or "unknown")


def _qualification_for_publication(
    passage: Mapping[str, Any], spec: Mapping[str, Any], *, now: datetime | None
) -> TemporalQualification:
    provenance = _provenance(passage)
    status = _field_status(passage, provenance, "publication")
    publication = normalize_temporal(passage.get("published_at"))
    if publication is None or status in _BLOCKING_STATUSES:
        reason = (
            "explicit_publication_authority_invalid_or_conflicting"
            if status in _BLOCKING_STATUSES
            else "missing_publication_authority"
        )
        return TemporalQualification("unresolved", "publication_within", reason)
    if not _in_window(publication, spec.get("time_window"), now=now):
        return TemporalQualification(
            "violates",
            "publication_within",
            "explicit_publication_out_of_window",
            publication.isoformat(),
        )
    return TemporalQualification(
        "satisfies",
        "publication_within",
        "authoritative_publication_in_window",
        publication.isoformat(),
    )


def _freshness_max_age(spec: Mapping[str, Any]) -> int | None:
    ages = [
        int(item["max_age_days"])
        for item in spec.get("freshness_requirements", ())
        if isinstance(item, Mapping) and item.get("max_age_days") is not None
    ]
    return min(ages) if ages else None


def _qualification_for_publication_or_update(
    passage: Mapping[str, Any], spec: Mapping[str, Any], *, now: datetime | None
) -> TemporalQualification:
    provenance = _provenance(passage)
    publication_status = _field_status(passage, provenance, "publication")
    update_status = _field_status(passage, provenance, "update")
    publication = normalize_temporal(passage.get("published_at"))
    update = normalize_temporal(
        passage.get("updated_at") or passage.get("last_modified")
    )
    if publication_status in _BLOCKING_STATUSES or update_status in _BLOCKING_STATUSES:
        return TemporalQualification(
            "unresolved",
            "publication_or_update_within",
            "explicit_publication_or_update_authority_invalid_or_conflicting",
        )

    max_age = _freshness_max_age(spec)
    if max_age is not None:
        reference = _reference(now)
        cutoff = reference - timedelta(days=max_age)
        qualifying = [
            value
            for value in (publication, update)
            if value is not None and cutoff <= value <= reference
        ]
        if qualifying:
            value = max(qualifying)
            return TemporalQualification(
                "satisfies",
                "publication_or_update_within",
                "authoritative_publication_or_update_is_fresh",
                value.isoformat(),
            )
        if publication is not None and update is not None:
            return TemporalQualification(
                "violates",
                "publication_or_update_within",
                "authoritative_publication_and_update_are_stale_or_future",
            )
        missing = (
            "missing_publication_authority"
            if publication is None and update is not None
            else "missing_update_authority"
            if update is None and publication is not None
            else "missing_publication_or_update_authority"
        )
        return TemporalQualification(
            "unresolved", "publication_or_update_within", missing
        )

    window = spec.get("time_window")
    values = [value for value in (publication, update) if value is not None]
    qualifying = [value for value in values if _in_window(value, window, now=now)]
    if qualifying:
        value = max(qualifying)
        return TemporalQualification(
            "satisfies",
            "publication_or_update_within",
            "authoritative_publication_or_update_in_window",
            value.isoformat(),
        )
    if publication is not None and update is not None:
        return TemporalQualification(
            "violates",
            "publication_or_update_within",
            "authoritative_publication_and_update_out_of_window",
        )
    return TemporalQualification(
        "unresolved",
        "publication_or_update_within",
        "missing_publication_or_update_authority",
    )


def _qualification_for_event(
    passage: Mapping[str, Any], spec: Mapping[str, Any], *, now: datetime | None
) -> TemporalQualification:
    provenance = _provenance(passage)
    status = str(provenance.get("event_status") or "unknown")
    event_at = normalize_temporal(provenance.get("event_at") or passage.get("event_at"))
    if event_at is None or status in _BLOCKING_STATUSES:
        reason = (
            "explicit_event_authority_invalid_or_conflicting"
            if status in _BLOCKING_STATUSES
            else "event_time_unresolved"
        )
        return TemporalQualification("unresolved", "event_within", reason)
    if not _in_window(event_at, spec.get("time_window"), now=now):
        return TemporalQualification(
            "violates",
            "event_within",
            "authoritative_event_out_of_window",
            event_at.isoformat(),
        )
    return TemporalQualification(
        "satisfies",
        "event_within",
        "authoritative_event_in_window",
        event_at.isoformat(),
    )


def _qualification_for_as_of(
    passage: Mapping[str, Any], spec: Mapping[str, Any], *, now: datetime | None
) -> TemporalQualification:
    provenance = _provenance(passage)
    start, end = _window_bounds(spec.get("time_window"))
    if start is None and end is None:
        return TemporalQualification(
            "unresolved", "current_as_of", "as_of_state_unresolved"
        )
    as_of_start = start or (end - timedelta(days=1) if end is not None else None)
    as_of_end = end or (start + timedelta(days=1) if start is not None else None)
    valid_from = normalize_temporal(provenance.get("state_valid_from"))
    valid_through = normalize_temporal(provenance.get("state_valid_through"))
    if valid_from is not None and valid_through is not None:
        if as_of_start is not None and as_of_end is not None:
            if valid_from < as_of_end and valid_through >= as_of_start:
                return TemporalQualification(
                    "satisfies",
                    "current_as_of",
                    "authoritative_state_interval_covers_as_of",
                    valid_from.isoformat(),
                )
            return TemporalQualification(
                "violates",
                "current_as_of",
                "authoritative_state_interval_excludes_as_of",
            )

    observed = normalize_temporal(
        provenance.get("state_observed_at") or passage.get("state_observed_at")
    )
    if observed is not None and as_of_start is not None and as_of_end is not None:
        if as_of_start <= observed < as_of_end and observed <= _reference(now):
            return TemporalQualification(
                "satisfies",
                "current_as_of",
                "source_state_observed_on_requested_as_of_date",
                observed.isoformat(),
            )
    return TemporalQualification(
        "unresolved", "current_as_of", "as_of_state_unresolved"
    )


def passage_temporal_qualification(
    passage: Mapping[str, Any],
    spec: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> TemporalQualification:
    """Return satisfies/violates/unresolved for this passage and obligation."""

    basis = resolved_temporal_basis(spec)
    if basis == "none":
        return TemporalQualification(
            "satisfies", "none", "no_temporal_obligation"
        )
    if basis == "publication_within":
        return _qualification_for_publication(passage, spec, now=now)
    if basis == "publication_or_update_within":
        return _qualification_for_publication_or_update(passage, spec, now=now)
    if basis == "event_within":
        return _qualification_for_event(passage, spec, now=now)
    if basis == "current_as_of":
        return _qualification_for_as_of(passage, spec, now=now)
    if basis == "conjunctive":
        publication = _qualification_for_publication(passage, spec, now=now)
        if publication.status != "satisfies":
            return TemporalQualification(
                publication.status,
                "conjunctive",
                publication.reason,
                publication.authoritative_time,
            )
        freshness = _qualification_for_publication_or_update(passage, spec, now=now)
        return TemporalQualification(
            freshness.status,
            "conjunctive",
            freshness.reason,
            freshness.authoritative_time,
        )
    raise ValueError(f"unsupported temporal basis: {basis}")


def passage_temporally_qualifies(
    passage: Mapping[str, Any],
    spec: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Compatibility wrapper around typed temporal qualification."""

    return passage_temporal_qualification(passage, spec, now=now).status == "satisfies"


__all__ = [
    "TemporalQualification",
    "freshness_satisfied",
    "has_temporal_obligations",
    "normalize_temporal",
    "parse_bound",
    "passage_temporal_qualification",
    "passage_temporally_qualifies",
    "publication_in_window",
    "resolved_temporal_basis",
]
