"""Public bounds, cursors, and JSON helpers for database-native inspection."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from .tokenizer_registry import get_tokenizer

_SCHEMA_VERSION = "database-native-inspection-v1"
_CURSOR_VERSION = 2
_MAX_PAGE_SIZE = 100
_MAX_PASSAGE_RECORDS = 100
_MAX_PASSAGE_CHARS = 64_000
_MAX_PASSAGE_TOKENS = 16_000
_MAX_REPLAY_BYTES = 4 * 1024 * 1024
_MAX_IDENTITY_ITEMS = 100
_MAX_METADATA_CHARS = 8_000
_MAX_TEXT_FIELD_CHARS = 4_000
_MAX_HISTORY_OUTPUT_CHARS = 256_000
_MAX_INSPECTION_OUTPUT_CHARS = 128_000
_MAX_SCRAPE_OUTPUT_CHARS = 256_000


class InspectionError(RuntimeError):
    """Base class for bounded database-native inspection failures."""


class InspectionNotFoundError(InspectionError):
    """An authoritative identifier was not found."""


class InspectionIntegrityError(InspectionError):
    """Retained payload bytes do not match PostgreSQL metadata."""


class InspectionBoundError(InspectionError):
    """A request or response exceeds a public output bound."""


@dataclass(frozen=True)
class PageRequest:
    limit: int = 20
    cursor: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= _MAX_PAGE_SIZE:
            raise ValueError(f"limit must be between 1 and {_MAX_PAGE_SIZE}")


@dataclass(frozen=True)
class PassageBounds:
    limit: int = 20
    max_chars: int = 20_000
    max_tokens: int = 4_000
    cursor: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= _MAX_PASSAGE_RECORDS:
            raise ValueError(f"limit must be between 1 and {_MAX_PASSAGE_RECORDS}")
        if not 1 <= self.max_chars <= _MAX_PASSAGE_CHARS:
            raise ValueError(f"max_chars must be between 1 and {_MAX_PASSAGE_CHARS}")
        if not 1 <= self.max_tokens <= _MAX_PASSAGE_TOKENS:
            raise ValueError(f"max_tokens must be between 1 and {_MAX_PASSAGE_TOKENS}")


def _json_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def _scope_fingerprint(kind: str, **values: Any) -> str:
    payload = json.dumps(
        {"kind": kind, **_json_value(values)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _encode_payload(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"version": _CURSOR_VERSION, **payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def _decode_payload(kind: str, scope: str, value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(value + padding))
        if payload.get("version") != _CURSOR_VERSION:
            raise ValueError("cursor version mismatch")
        if payload.get("kind") != kind:
            raise ValueError("cursor kind mismatch")
        if payload.get("scope") != scope:
            raise ValueError("cursor scope mismatch")
        return payload
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid pagination cursor") from exc


def _encode_cursor(
    kind: str,
    timestamp: datetime,
    row_id: UUID,
    *,
    scope: str | None = None,
) -> str:
    return _encode_payload(
        {
            "kind": kind,
            "scope": scope or _scope_fingerprint(kind),
            "timestamp": timestamp.isoformat(),
            "id": str(row_id),
        }
    )


def _decode_cursor(
    kind: str,
    value: str | None,
    *,
    scope: str | None = None,
) -> tuple[datetime, UUID] | None:
    payload = _decode_payload(kind, scope or _scope_fingerprint(kind), value)
    if payload is None:
        return None
    try:
        return (
            datetime.fromisoformat(str(payload["timestamp"])),
            UUID(str(payload["id"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid pagination cursor") from exc


def _encode_chunk_cursor(
    kind: str,
    document_id: UUID,
    ordinal: int,
    row_id: UUID,
    *,
    scope: str,
    offset: int = 0,
) -> str:
    return _encode_payload(
        {
            "kind": kind,
            "scope": scope,
            "document_id": str(document_id),
            "ordinal": int(ordinal),
            "id": str(row_id),
            "offset": int(offset),
        }
    )


def _decode_chunk_cursor(
    kind: str,
    value: str | None,
    *,
    scope: str,
) -> tuple[UUID, int, UUID, int] | None:
    payload = _decode_payload(kind, scope, value)
    if payload is None:
        return None
    try:
        offset = int(payload.get("offset", 0))
        if offset < 0:
            raise ValueError("negative offset")
        return (
            UUID(str(payload["document_id"])),
            int(payload["ordinal"]),
            UUID(str(payload["id"])),
            offset,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid pagination cursor") from exc


def _encode_rank_cursor(
    kind: str,
    rank: float,
    row_id: UUID,
    *,
    scope: str,
    offset: int = 0,
) -> str:
    return _encode_payload(
        {
            "kind": kind,
            "scope": scope,
            "rank": repr(float(rank)),
            "id": str(row_id),
            "offset": int(offset),
        }
    )


def _decode_rank_cursor(
    kind: str,
    value: str | None,
    *,
    scope: str,
) -> tuple[float, UUID, int] | None:
    payload = _decode_payload(kind, scope, value)
    if payload is None:
        return None
    try:
        offset = int(payload.get("offset", 0))
        if offset < 0:
            raise ValueError("negative offset")
        return float(payload["rank"]), UUID(str(payload["id"])), offset
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid pagination cursor") from exc


def _bounded_text(
    value: Any,
    limit: int = _MAX_TEXT_FIELD_CHARS,
) -> dict[str, Any] | str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return {
        "preview": text[:limit],
        "original_char_count": len(text),
        "truncated": True,
    }


def _bounded_json(value: Any, limit: int = _MAX_METADATA_CHARS) -> Any:
    normalized = _json_value(value)
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    if len(encoded) <= limit:
        return normalized
    return {
        "preview": encoded[:limit],
        "original_char_count": len(encoded),
        "truncated": True,
    }


def _bounded_identities(
    values: Sequence[Any],
    *,
    total: int | None = None,
) -> dict[str, Any]:
    normalized = [str(value) for value in values[:_MAX_IDENTITY_ITEMS]]
    count = int(total if total is not None else len(values))
    return {
        "items": normalized,
        "total_count": count,
        "returned_count": len(normalized),
        "truncated": count > len(normalized),
    }


def _finalize_payload(payload: Mapping[str, Any], *, max_chars: int) -> dict[str, Any]:
    result = _json_value(dict(payload))
    result["output_bounds"] = {
        "max_serialized_chars": max_chars,
        "serialized_chars": 0,
    }
    for _ in range(3):
        encoded = json.dumps(
            result,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        result["output_bounds"]["serialized_chars"] = len(encoded)
    encoded = json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    if len(encoded) > max_chars:
        raise InspectionBoundError(
            f"serialized response exceeds public bound: {len(encoded)} > {max_chars}"
        )
    return result


def _page(
    kind: str,
    rows: Sequence[Mapping[str, Any]],
    limit: int,
    *,
    scope: str,
    timestamp_field: str = "created_at",
) -> dict[str, Any]:
    selected = list(rows[:limit])
    truncated = len(rows) > limit
    next_cursor = None
    if truncated and selected:
        last = selected[-1]
        next_cursor = _encode_cursor(
            kind,
            last[timestamp_field],
            UUID(str(last["id"])),
            scope=scope,
        )
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": kind,
        "items": [_json_value(item) for item in selected],
        "item_count": len(selected),
        "truncated": truncated,
        "next_cursor": next_cursor,
    }


def _rows(cursor: Any, names: Sequence[str]) -> list[dict[str, Any]]:
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _largest_prefix(
    text: str,
    *,
    max_chars: int,
    max_tokens: int,
    tokenizer_name: str,
) -> tuple[str, int]:
    if not text or max_chars <= 0 or max_tokens <= 0:
        return "", 0
    try:
        tokenizer = get_tokenizer(tokenizer_name)
    except KeyError as exc:
        raise InspectionIntegrityError(
            f"stored chunk references unknown tokenizer: {tokenizer_name}"
        ) from exc
    upper = min(len(text), max_chars)
    low = 0
    high = upper
    best = 0
    best_tokens = 0
    while low <= high:
        mid = (low + high) // 2
        count = tokenizer.count(text[:mid])
        if count <= max_tokens:
            best = mid
            best_tokens = count
            low = mid + 1
        else:
            high = mid - 1
    if best == 0 and text:
        raise InspectionBoundError(
            "max_tokens is too small to return the next character with the "
            "stored tokenizer"
        )
    return text[:best], best_tokens


def _bound_passage_rows(
    rows: Sequence[Mapping[str, Any]],
    bounds: PassageBounds,
    *,
    start_offset: int = 0,
) -> tuple[
    list[dict[str, Any]],
    int,
    int,
    str | None,
    tuple[dict[str, Any], int] | None,
]:
    """Return a lossless bounded page and an exact resume position.

    A non-zero resume offset points back into the same chunk. An offset of zero
    means the row was fully consumed and the next query must resume after it.
    """

    selected: list[dict[str, Any]] = []
    chars = 0
    tokens = 0
    exhausted_by = None
    resume: tuple[dict[str, Any], int] | None = None
    for index, raw_item in enumerate(rows):
        if len(selected) >= bounds.limit:
            exhausted_by = "limit"
            break
        item = dict(raw_item)
        text = str(item.get("text") or "")
        offset = start_offset if index == 0 else 0
        if offset > len(text):
            raise ValueError("pagination cursor offset exceeds chunk length")
        remaining_text = text[offset:]
        remaining_chars = bounds.max_chars - chars
        remaining_tokens = bounds.max_tokens - tokens
        if remaining_chars <= 0:
            exhausted_by = "max_chars"
            break
        if remaining_tokens <= 0:
            exhausted_by = "max_tokens"
            break
        tokenizer_name = str(item.get("tokenizer_name") or "cl100k_base")
        clipped, returned_tokens = _largest_prefix(
            remaining_text,
            max_chars=remaining_chars,
            max_tokens=remaining_tokens,
            tokenizer_name=tokenizer_name,
        )
        next_offset = offset + len(clipped)
        item["text"] = clipped
        item["text_offset"] = offset
        item["next_text_offset"] = next_offset
        item["text_truncated"] = next_offset < len(text)
        item["returned_token_count"] = returned_tokens
        item["token_count_truncated"] = item["text_truncated"]
        selected.append(item)
        chars += len(clipped)
        tokens += returned_tokens
        if item["text_truncated"]:
            exhausted_by = (
                "max_chars" if chars >= bounds.max_chars else "max_tokens"
            )
            resume = (item, next_offset)
            break
        resume = (item, 0)
    if exhausted_by is None and len(rows) > len(selected):
        exhausted_by = "limit"
    return selected, chars, tokens, exhausted_by, resume


def _bound_scrape_result(value: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
    raw_items = list(value.get("items") or [])
    items: list[dict[str, Any]] = []
    for raw in raw_items[:20]:
        item = dict(raw)
        chunk_ids = list(item.pop("chunk_ids", ()) or ())
        item["chunk_ids"] = _bounded_identities(chunk_ids)
        for name, field_value in list(item.items()):
            if isinstance(field_value, str):
                item[name] = _bounded_text(field_value)
            elif isinstance(field_value, Mapping):
                item[name] = _bounded_json(field_value)
        items.append(_json_value(item))
    status = str(value.get("status") or "failed")
    return _finalize_payload(
        {
            "schema_version": _SCHEMA_VERSION,
            "kind": kind,
            "status": status,
            "failure_stage": None if status == "complete" else "extraction",
            "run_id": value.get("run_id"),
            "invocation_id": value.get("invocation_id"),
            "idempotency_key": value.get("idempotency_key"),
            "replayed": bool(value.get("replayed")),
            "items": items,
            "item_count": len(raw_items),
            "items_truncated": len(raw_items) > len(items),
        },
        max_chars=_MAX_SCRAPE_OUTPUT_CHARS,
    )
