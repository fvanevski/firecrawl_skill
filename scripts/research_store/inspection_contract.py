"""Public bounds, cursors, and JSON helpers for database-native inspection."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

_SCHEMA_VERSION = "database-native-inspection-v1"
_MAX_PAGE_SIZE = 100
_MAX_PASSAGE_RECORDS = 100
_MAX_PASSAGE_CHARS = 64_000
_MAX_PASSAGE_TOKENS = 16_000
_MAX_REPLAY_BYTES = 4 * 1024 * 1024


class InspectionError(RuntimeError):
    """Base class for bounded database-native inspection failures."""


class InspectionNotFoundError(InspectionError):
    """An authoritative identifier was not found."""


class InspectionIntegrityError(InspectionError):
    """Retained payload bytes do not match PostgreSQL metadata."""


class InspectionBoundError(InspectionError):
    """A request exceeds a public output bound."""


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
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _encode_cursor(kind: str, timestamp: datetime, row_id: UUID) -> str:
    payload = json.dumps(
        {"kind": kind, "timestamp": timestamp.isoformat(), "id": str(row_id)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(kind: str, value: str | None) -> tuple[datetime, UUID] | None:
    if value is None:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(value + padding))
        if payload.get("kind") != kind:
            raise ValueError("cursor kind mismatch")
        timestamp = datetime.fromisoformat(str(payload["timestamp"]))
        return timestamp, UUID(str(payload["id"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid pagination cursor") from exc


def _encode_chunk_cursor(
    kind: str, document_id: UUID, ordinal: int, row_id: UUID
) -> str:
    payload = json.dumps(
        {
            "kind": kind,
            "document_id": str(document_id),
            "ordinal": int(ordinal),
            "id": str(row_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_chunk_cursor(kind: str, value: str | None) -> tuple[UUID, int, UUID] | None:
    if value is None:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(value + padding))
        if payload.get("kind") != kind:
            raise ValueError("cursor kind mismatch")
        return (
            UUID(str(payload["document_id"])),
            int(payload["ordinal"]),
            UUID(str(payload["id"])),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid pagination cursor") from exc


def _encode_rank_cursor(kind: str, rank: float, row_id: UUID) -> str:
    payload = json.dumps(
        {"kind": kind, "rank": repr(float(rank)), "id": str(row_id)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_rank_cursor(kind: str, value: str | None) -> tuple[float, UUID] | None:
    if value is None:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(value + padding))
        if payload.get("kind") != kind:
            raise ValueError("cursor kind mismatch")
        return float(payload["rank"]), UUID(str(payload["id"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid pagination cursor") from exc


def _page(
    kind: str,
    rows: Sequence[Mapping[str, Any]],
    limit: int,
    *,
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


def _bound_passage_rows(
    rows: Sequence[Mapping[str, Any]], bounds: PassageBounds
) -> tuple[list[dict[str, Any]], int, int, str | None]:
    selected: list[dict[str, Any]] = []
    chars = 0
    tokens = 0
    exhausted_by = None
    for raw_item in rows[: bounds.limit]:
        item = dict(raw_item)
        text = str(item.get("text") or "")
        token_count = max(0, int(item.get("token_count") or 0))
        remaining_chars = bounds.max_chars - chars
        remaining_tokens = bounds.max_tokens - tokens
        if remaining_chars <= 0:
            exhausted_by = "max_chars"
            break
        if remaining_tokens <= 0:
            exhausted_by = "max_tokens"
            break

        allowed_chars = min(len(text), remaining_chars)
        returned_tokens = token_count
        token_truncated = False
        if token_count > remaining_tokens:
            token_truncated = True
            returned_tokens = remaining_tokens
            if token_count > 0 and text:
                proportional = max(1, int(len(text) * remaining_tokens / token_count))
                allowed_chars = min(allowed_chars, proportional)

        clipped = text[:allowed_chars]
        item["text"] = clipped
        item["text_truncated"] = len(clipped) < len(text)
        item["returned_token_count"] = returned_tokens
        item["token_count_truncated"] = token_truncated
        selected.append(item)
        chars += len(clipped)
        tokens += returned_tokens
        if item["text_truncated"]:
            exhausted_by = "max_tokens" if token_truncated else "max_chars"
            break
    return selected, chars, tokens, exhausted_by
