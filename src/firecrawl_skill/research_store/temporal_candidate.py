"""Canonical normalization of candidate and document temporal metadata."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
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
_VISIBLE_UPDATE = re.compile(
    r"\b(?:last\s+updated|updated)\s*(?:on\s+|:\s*|-\s*)?"
    r"(?P<value>(?:\d{4}-\d{2}-\d{2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{1,2},\s+\d{4}(?:\s+(?:at\s+)?\d{1,2}:\d{2}\s*(?:AM|PM))?))",
    re.IGNORECASE,
)
_VISIBLE_PUBLICATION = re.compile(
    r"\bpublished\s*(?:on\s+|:\s*|-\s*)?"
    r"(?P<value>(?:\d{4}-\d{2}-\d{2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{1,2},\s+\d{4}(?:\s+(?:at\s+)?\d{1,2}:\d{2}\s*(?:AM|PM))?))",
    re.IGNORECASE,
)
_PUBLICATION_MARKERS = {
    "article:published_time",
    "datepublished",
    "date_published",
    "publication_date",
    "publishdate",
    "pubdate",
}
_UPDATE_MARKERS = {
    "article:modified_time",
    "datemodified",
    "dateupdated",
    "last-modified",
    "last_modified",
    "modified_time",
    "updated_time",
}
_MAX_STRUCTURED_MAPPINGS = 128
_MAX_STRUCTURED_SEGMENTS = 64


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
            normalized = re.sub(r"\s+at\s+", " ", raw, flags=re.IGNORECASE)
            parsed = None
            for pattern in (
                "%B %d, %Y %I:%M %p",
                "%b %d, %Y %I:%M %p",
                "%B %d, %Y",
                "%b %d, %Y",
            ):
                try:
                    parsed = datetime.strptime(normalized, pattern).replace(
                        tzinfo=timezone.utc
                    )
                    break
                except ValueError:
                    continue
            if parsed is None:
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
    """Return a bounded recursive census of mappings in structured metadata."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    result: list[Mapping[str, Any]] = []
    stack: list[Any] = [value]
    seen: set[int] = set()
    while stack and len(result) < _MAX_STRUCTURED_MAPPINGS:
        current = stack.pop()
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(current)
            children = list(current.values())
            stack.extend(reversed(children))
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            stack.extend(reversed(list(current)))
    return result


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
                pub, upd = _mapping_signals(structured, source=f"json_ld:{key}:{index}")
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
    parsed_values: set[datetime] = set()
    for item in entries:
        parsed = parse_provider_datetime(item.get("parsed"))
        if parsed is None:
            return None, "explicit_provider_invalid"
        parsed_values.add(parsed.astimezone(timezone.utc))
    if len(parsed_values) != 1:
        return None, "explicit_provider_conflict"
    return next(iter(parsed_values)), "explicit_provider_valid"


class _TemporalHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.publications: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.text_parts: list[str] = []

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(key).casefold(): str(value or "") for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = self._attrs(attrs)
        lowered = tag.casefold()
        marker = (
            values.get("property") or values.get("itemprop") or values.get("name") or ""
        ).casefold()
        if lowered == "meta":
            content = values.get("content")
            if marker in _PUBLICATION_MARKERS:
                _signal(
                    self.publications,
                    signal_class="publication",
                    source="html_meta",
                    field=marker,
                    value=content,
                )
            elif marker in _UPDATE_MARKERS:
                _signal(
                    self.updates,
                    signal_class="update",
                    source="html_meta",
                    field=marker,
                    value=content,
                )
        elif lowered == "time":
            temporal = values.get("datetime")
            if marker in _PUBLICATION_MARKERS:
                _signal(
                    self.publications,
                    signal_class="publication",
                    source="html_time",
                    field=marker,
                    value=temporal,
                )
            elif marker in _UPDATE_MARKERS:
                _signal(
                    self.updates,
                    signal_class="update",
                    source="html_time",
                    field=marker,
                    value=temporal,
                )

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text:
            self.text_parts.append(text)


def _segment_payload(
    structured: Mapping[str, Any], *, source: str
) -> dict[str, Any] | None:
    publications, updates = _mapping_signals(structured, source=source)
    if not publications and not updates:
        return None
    publication, publication_status = _canonical_signal(publications)
    update, update_status = _canonical_signal(updates)
    segment_type = structured.get("@type")
    if isinstance(segment_type, Sequence) and not isinstance(
        segment_type, (str, bytes)
    ):
        segment_type = ",".join(str(value) for value in segment_type)
    return {
        "source": source,
        "type": str(segment_type or ""),
        "headline": str(structured.get("headline") or structured.get("name") or ""),
        "url": str(structured.get("url") or ""),
        "published_at": publication.isoformat() if publication is not None else None,
        "updated_at": update.isoformat() if update is not None else None,
        "publication_status": publication_status,
        "update_status": update_status,
        "publication_signals": publications,
        "update_signals": updates,
    }


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
    """Extract bounded explicit document publication/update provenance."""

    publications: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    structured_segments: list[dict[str, Any]] = []
    text = content.decode("utf-8", errors="replace")
    base_type = mime_type.split(";", 1)[0].strip().lower()

    if base_type in {"text/html", "application/xhtml+xml"}:
        parser = _TemporalHTMLParser()
        parser.feed(text)
        publications.extend(parser.publications)
        updates.extend(parser.updates)
        for script_index, match in enumerate(_JSON_LD_SCRIPT.finditer(text)):
            for structured_index, structured in enumerate(
                _structured_values(match.group("body"))
            ):
                source = f"document_json_ld:{script_index}:{structured_index}"
                pub, upd = _mapping_signals(structured, source=source)
                publications.extend(pub)
                updates.extend(upd)
                if len(structured_segments) < _MAX_STRUCTURED_SEGMENTS:
                    segment = _segment_payload(structured, source=source)
                    if segment is not None:
                        structured_segments.append(segment)
        visible = " ".join(parser.text_parts)
        if not updates:
            match = _VISIBLE_UPDATE.search(visible)
            if match is not None:
                _signal(
                    updates,
                    signal_class="update",
                    source="page_text_explicit_marker",
                    field="updated",
                    value=match.group("value"),
                )
        if not publications:
            match = _VISIBLE_PUBLICATION.search(visible)
            if match is not None:
                _signal(
                    publications,
                    signal_class="publication",
                    source="page_text_explicit_marker",
                    field="published",
                    value=match.group("value"),
                )
    elif base_type == "application/json":
        for structured_index, structured in enumerate(_structured_values(text)):
            source = f"document_json:{structured_index}"
            pub, upd = _mapping_signals(structured, source=source)
            publications.extend(pub)
            updates.extend(upd)
            if len(structured_segments) < _MAX_STRUCTURED_SEGMENTS:
                segment = _segment_payload(structured, source=source)
                if segment is not None:
                    structured_segments.append(segment)

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
        "structured_temporal_segments": structured_segments,
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
