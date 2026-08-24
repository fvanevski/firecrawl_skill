"""Canonical normalization of candidate and document temporal metadata."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

_PUBLICATION_KEYS = (
    "published_at",
    "publishedAt",
    "publishedDate",
    "published_date",
    "datePublished",
)
_UPDATE_KEYS = (
    "updated_at",
    "updatedAt",
    "updatedDate",
    "modified_at",
    "modifiedDate",
    "lastModified",
    "last_modified",
    "dateModified",
    "dateUpdated",
)
_JSON_LD_KEYS = ("jsonLd", "jsonld", "json_ld", "structuredData", "structured_data")
_JSON_LD_SCRIPT = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(?P<body>.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


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


def _signal(
    target: list[dict[str, Any]],
    *,
    signal_class: str,
    source: str,
    field: str,
    value: Any,
) -> None:
    if value in (None, ""):
        return
    parsed = parse_provider_datetime(value)
    target.append(
        {
            "signal_class": signal_class,
            "source": source,
            "field": field,
            "raw": str(value),
            "parsed": parsed.isoformat() if parsed is not None else None,
            "status": "valid" if parsed is not None else "invalid",
        }
    )


def _mapping_signals(
    mapping: Mapping[str, Any], *, source: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    publications: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    for key in _PUBLICATION_KEYS:
        _signal(
            publications,
            signal_class="publication",
            source=source,
            field=key,
            value=mapping.get(key),
        )
    for key in _UPDATE_KEYS:
        _signal(
            updates,
            signal_class="update",
            source=source,
            field=key,
            value=mapping.get(key),
        )
    return publications, updates


def _structured_values(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if isinstance(value, Mapping):
        result = [value]
        graph = value.get("@graph")
        if isinstance(graph, Sequence) and not isinstance(graph, (str, bytes)):
            result.extend(item for item in graph if isinstance(item, Mapping))
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _collect_raw_signals(
    raw: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    publications, updates = _mapping_signals(raw, source="provider_item")
    metadata = raw.get("metadata")
    if isinstance(metadata, Mapping):
        pub, upd = _mapping_signals(metadata, source="provider_metadata")
        publications.extend(pub)
        updates.extend(upd)

    containers = [raw]
    if isinstance(metadata, Mapping):
        containers.append(metadata)
    for container in containers:
        for key in _JSON_LD_KEYS:
            for index, structured in enumerate(_structured_values(container.get(key))):
                pub, upd = _mapping_signals(
                    structured, source=f"json_ld:{key}:{index}"
                )
                publications.extend(pub)
                updates.extend(upd)
    return publications, updates


def _canonical_signal(
    entries: Sequence[Mapping[str, Any]],
) -> tuple[datetime | None, str]:
    if not entries:
        return None, "unknown"
    if any(item.get("status") != "valid" for item in entries):
        return None, "explicit_provider_invalid"
    parsed_values = {
        str(item["parsed"])
        for item in entries
        if item.get("parsed") not in (None, "")
    }
    if len(parsed_values) != 1:
        return None, "explicit_provider_conflict"
    return parse_provider_datetime(next(iter(parsed_values))), "explicit_provider_valid"


def canonical_candidate_temporal(
    raw: Mapping[str, Any],
    *,
    stored_publication: Any = None,
    stored_signals: Mapping[str, Any] | None = None,
) -> tuple[datetime | None, dict[str, Any]]:
    """Separate explicit publication/update signals from ambiguous provider dates.

    Generic provider ``date`` never becomes publication. Invalid or conflicting
    explicit signals fail closed to unknown while retaining bounded provenance.
    """

    previous = dict(stored_signals or {})
    signals = dict(previous)
    for key in (
        "published_date",
        "updated_date",
        "provider_date",
        "publication_raw",
        "publication_status",
        "update_status",
        "publication_signals",
        "update_signals",
    ):
        signals.pop(key, None)

    publications, updates = _collect_raw_signals(raw)
    publication, publication_status = _canonical_signal(publications)
    update, update_status = _canonical_signal(updates)

    if not publications and stored_publication is not None:
        publication = parse_provider_datetime(stored_publication)
        publication_status = (
            "previous_explicit_provider" if publication is not None else "unknown"
        )
    if not updates and previous.get("updated_date") not in (None, ""):
        previous_update = parse_provider_datetime(previous.get("updated_date"))
        previous_status = previous.get("update_status")
        if previous_update is not None and previous_status in {
            "explicit_provider_valid",
            "previous_explicit_provider",
        }:
            update = previous_update
            update_status = "previous_explicit_provider"

    signals["publication_status"] = publication_status
    signals["update_status"] = update_status
    signals["publication_signals"] = [dict(item) for item in publications]
    signals["update_signals"] = [dict(item) for item in updates]
    if publications:
        signals["publication_raw"] = str(publications[0]["raw"])
    if publication is not None:
        signals["published_date"] = publication.isoformat()
    if update is not None:
        single_raw = updates[0]["raw"] if len(updates) == 1 else update.isoformat()
        signals["updated_date"] = str(single_raw)
    provider_date = raw.get("date")
    if provider_date in (None, ""):
        provider_date = previous.get("provider_date")
    if provider_date not in (None, ""):
        signals["provider_date"] = str(provider_date)
    return publication, signals


def extract_document_temporal_signals(
    content: bytes,
    *,
    mime_type: str,
    transport_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract explicit document publication/update provenance when available."""

    publications: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    text = content.decode("utf-8", errors="replace")
    base_type = mime_type.split(";", 1)[0].strip().lower()

    if base_type in {"text/html", "application/xhtml+xml"}:
        for index, match in enumerate(_JSON_LD_SCRIPT.finditer(text)):
            for structured_index, structured in enumerate(
                _structured_values(match.group("body"))
            ):
                pub, upd = _mapping_signals(
                    structured,
                    source=f"document_json_ld:{index}:{structured_index}",
                )
                publications.extend(pub)
                updates.extend(upd)
    elif base_type == "application/json":
        for index, structured in enumerate(_structured_values(text)):
            pub, upd = _mapping_signals(
                structured, source=f"document_json:{index}"
            )
            publications.extend(pub)
            updates.extend(upd)

    metadata = transport_metadata or {}
    headers = metadata.get("headers")
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if str(key).casefold() == "last-modified":
                _signal(
                    updates,
                    signal_class="update",
                    source="http_header",
                    field="Last-Modified",
                    value=value,
                )

    publication, publication_status = _canonical_signal(publications)
    update, update_status = _canonical_signal(updates)
    return {
        "publication_status": publication_status,
        "update_status": update_status,
        "published_at": publication.isoformat() if publication is not None else None,
        "updated_at": update.isoformat() if update is not None else None,
        "publication_signals": publications,
        "update_signals": updates,
    }


def ranking_safe_raw_item(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Prevent ranking compatibility code from treating generic date as publication."""
    result = dict(raw)
    result.pop("date", None)
    return result


__all__ = [
    "canonical_candidate_temporal",
    "extract_document_temporal_signals",
    "parse_provider_datetime",
    "ranking_safe_raw_item",
]
