"""Canonical normalization of provider candidate temporal metadata."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

_PUBLICATION_KEYS = ("published_at", "publishedDate", "published_date")
_UPDATE_KEYS = (
    "updated_at",
    "updatedAt",
    "modified_at",
    "modifiedDate",
    "lastModified",
    "last_modified",
)


def _first(data: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def parse_provider_datetime(value: Any) -> datetime | None:
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


def canonical_candidate_temporal(
    raw: Mapping[str, Any],
    *,
    stored_publication: Any = None,
    stored_signals: Mapping[str, Any] | None = None,
) -> tuple[datetime | None, dict[str, Any]]:
    """Separate publication, update, ambiguous provider date, and retrieval.

    ``stored_publication`` is trusted only as previously canonicalized explicit
    publication authority. Generic provider ``date`` never becomes publication.
    """
    signals = dict(stored_signals or {})
    # Remove legacy conflation before rebuilding canonical temporal authority.
    for key in (
        "published_date",
        "updated_date",
        "provider_date",
        "publication_raw",
        "publication_status",
    ):
        signals.pop(key, None)

    publication_raw = _first(raw, _PUBLICATION_KEYS)
    update_raw = _first(raw, _UPDATE_KEYS)
    provider_date = raw.get("date")
    parsed_publication = parse_provider_datetime(publication_raw)
    publication = parsed_publication or stored_publication

    if publication_raw is not None:
        signals["publication_raw"] = str(publication_raw)
        signals["publication_status"] = (
            "explicit_provider_valid"
            if parsed_publication is not None
            else "explicit_provider_invalid"
        )
    elif stored_publication is not None:
        signals["publication_status"] = "previous_explicit_provider"
    else:
        signals["publication_status"] = "unknown"
    if publication is not None:
        signals["published_date"] = publication.isoformat()
    if update_raw is not None:
        signals["updated_date"] = str(update_raw)
    if provider_date not in (None, ""):
        signals["provider_date"] = str(provider_date)
    return publication, signals


def ranking_safe_raw_item(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Prevent ranking compatibility code from treating generic date as publication."""
    result = dict(raw)
    result.pop("date", None)
    return result


__all__ = [
    "canonical_candidate_temporal",
    "parse_provider_datetime",
    "ranking_safe_raw_item",
]
