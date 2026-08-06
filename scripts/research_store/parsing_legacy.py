from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .domain import Block, Chunk

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_LIST = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")
_QUOTE = re.compile(r"^\s*>\s?")
_NO_RESULTS = re.compile(r"^\s*no results found\.?\s*$", re.IGNORECASE)
_RESULT_KEYS = ("data", "results", "candidates", "items")


def extract_search_response_items(data: Any) -> list[Any]:
    """Return ordered candidates from supported Firecrawl search envelopes."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []

    for key in _RESULT_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            return value
        if key == "data" and isinstance(value, dict):
            items: list[Any] = []
            for source in ("web", "news", "images"):
                source_items = value.get(source)
                if isinstance(source_items, list):
                    items.extend(source_items)
            return items
    return []


def _empty_search_summary() -> dict[str, Any]:
    return {"result_count": 0, "sample_candidates": []}


def _is_no_results_message(value: Any) -> bool:
    return isinstance(value, str) and _NO_RESULTS.fullmatch(value) is not None


def _provider_declared_empty(data: Any) -> bool:
    """Return whether a JSON provider envelope explicitly declares no results."""
    if not isinstance(data, dict):
        return False
    message = data.get("error") or data.get("message") or data.get("detail")
    if not _is_no_results_message(message):
        return False
    if data.get("success") is True or extract_search_response_items(data):
        return False

    found_result_collection = False
    for key in _RESULT_KEYS:
        if key not in data:
            continue
        found_result_collection = True
        value = data[key]
        if isinstance(value, list):
            continue
        if key == "data" and isinstance(value, dict):
            known_sources = {"web", "news", "images"}
            if set(value).issubset(known_sources) and all(
                isinstance(source_items, list) for source_items in value.values()
            ):
                continue
        return False
    return found_result_collection


def _has_supported_result_envelope(data: Any) -> bool:
    if isinstance(data, list):
        return True
    if not isinstance(data, dict):
        return False

    for key in _RESULT_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, list):
            return True
        if key == "data" and isinstance(value, dict):
            known_sources = {"web", "news", "images"}
            return set(value).issubset(known_sources) and all(
                isinstance(source_items, list) for source_items in value.values()
            )
        return False
    return False


def structural_blocks(markdown: str) -> list[Block]:
    """Parse stable, ordered Markdown blocks without losing source offsets."""
    blocks: list[Block] = []
    headings: list[str] = []
    offset = 0
    paragraph: list[tuple[str, int, int]] = []
    code: list[tuple[str, int, int]] = []
    in_code = False

    def emit(lines, block_type):
        if not lines:
            return
        text = "".join(item[0] for item in lines).strip("\n")
        if text:
            blocks.append(
                Block(
                    len(blocks),
                    block_type,
                    text,
                    tuple(headings),
                    lines[0][1],
                    lines[-1][2],
                )
            )
        lines.clear()

    for line in markdown.splitlines(keepends=True):
        start, end = offset, offset + len(line)
        offset = end
        if _FENCE.match(line):
            emit(paragraph, "paragraph")
            code.append((line, start, end))
            if in_code:
                emit(code, "code")
            in_code = not in_code
            continue
        if in_code:
            code.append((line, start, end))
            continue
        match = _HEADING.match(line)
        if match:
            emit(paragraph, "paragraph")
            level, title = len(match.group(1)), match.group(2).strip()
            headings[level - 1 :] = [title]
            blocks.append(
                Block(len(blocks), "heading", title, tuple(headings), start, end)
            )
        elif not line.strip():
            emit(paragraph, "paragraph")
        elif _LIST.match(line):
            emit(paragraph, "paragraph")
            blocks.append(
                Block(
                    len(blocks),
                    "list_item",
                    _LIST.sub("", line).strip(),
                    tuple(headings),
                    start,
                    end,
                )
            )
        elif _QUOTE.match(line):
            emit(paragraph, "paragraph")
            blocks.append(
                Block(
                    len(blocks),
                    "quotation",
                    _QUOTE.sub("", line).strip(),
                    tuple(headings),
                    start,
                    end,
                )
            )
        elif "|" in line and line.count("|") >= 2:
            emit(paragraph, "paragraph")
            blocks.append(
                Block(
                    len(blocks), "table_row", line.strip(), tuple(headings), start, end
                )
            )
        elif re.match(r"^!\[[^]]*\]\(", line):
            emit(paragraph, "paragraph")
            blocks.append(
                Block(len(blocks), "caption", line.strip(), tuple(headings), start, end)
            )
        else:
            paragraph.append((line, start, end))
    emit(code, "code")
    emit(paragraph, "paragraph")
    return blocks


def deterministic_chunks(blocks: list[Block], max_chars: int = 3000) -> list[Chunk]:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    chunks: list[Chunk] = []
    current: list[Block] = []
    length = 0

    def emit():
        nonlocal length
        if not current:
            return
        text = "\n\n".join(block.text for block in current)
        chunks.append(
            Chunk(
                ordinal=len(chunks),
                text=text,
                content_sha256=hashlib.sha256(text.encode()).hexdigest(),
                first_block_ordinal=current[0].ordinal,
                last_block_ordinal=current[-1].ordinal,
                token_count=max(1, (len(text) + 3) // 4),
                heading_path=current[-1].heading_path,
            )
        )
        current.clear()
        length = 0

    for block in blocks:
        added = len(block.text) + (2 if current else 0)
        if current and length + added > max_chars:
            emit()
        current.append(block)
        length += added
    emit()
    return chunks


def parse_raw_search_response(
    raw_payload: bytes | str,
    http_status: int | None = None,
    parser_version: str = "firecrawl-search-v1",
) -> tuple[str, int, dict[str, Any], str | None]:
    """Parse raw search response payload and classify response status.

    Returns:
        (status, result_count, payload_summary, error_message)

    Statuses:
        - 'succeeded': Valid response containing one or more candidates
        - 'empty': Valid response containing zero candidates
        - 'provider_error': Provider returned an HTTP or payload-declared failure
        - 'parse_error': Payload was malformed or violated the response contract
    """
    if isinstance(raw_payload, str):
        text_content = raw_payload
    elif http_status is not None and http_status >= 400:
        text_content = raw_payload.decode("utf-8", errors="replace")
    else:
        try:
            text_content = raw_payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            return (
                "parse_error",
                0,
                {"raw_length": len(raw_payload)},
                f"Failed to decode search response as UTF-8: {exc}",
            )

    if http_status is not None and http_status >= 400:
        error_msg: Any = None
        try:
            error_data = json.loads(text_content)
        except json.JSONDecodeError:
            error_msg = text_content.strip() or None
        else:
            if isinstance(error_data, dict):
                error_msg = (
                    error_data.get("error")
                    or error_data.get("message")
                    or error_data.get("detail")
                )
        error_msg = error_msg or f"Provider HTTP {http_status}"
        return (
            "provider_error",
            0,
            {"http_status": http_status, "error": str(error_msg)},
            str(error_msg),
        )

    if _is_no_results_message(text_content):
        return ("empty", 0, _empty_search_summary(), None)

    try:
        data = json.loads(text_content)
    except json.JSONDecodeError as exc:
        sample = text_content[:200]
        return (
            "parse_error",
            0,
            {"raw_sample": sample},
            f"Failed to parse search response as JSON: {exc}",
        )

    if not isinstance(data, (dict, list)):
        return (
            "parse_error",
            0,
            {"type": type(data).__name__},
            "Search response JSON root must be an object or array",
        )

    if _provider_declared_empty(data):
        return ("empty", 0, _empty_search_summary(), None)

    if isinstance(data, dict) and _is_no_results_message(
        data.get("error") or data.get("message") or data.get("detail")
    ):
        return (
            "parse_error",
            0,
            {"keys": sorted(data)},
            "Provider no-results response violated the supported empty-result contract",
        )

    if isinstance(data, dict) and (data.get("success") is False or "error" in data):
        error_msg = (
            data.get("error") or data.get("message") or "Provider reported failure"
        )
        return (
            "provider_error",
            0,
            {"error": error_msg},
            str(error_msg),
        )

    if not _has_supported_result_envelope(data):
        return (
            "parse_error",
            0,
            {"keys": sorted(data) if isinstance(data, dict) else []},
            "Search response JSON does not contain a supported result collection",
        )

    items = extract_search_response_items(data)

    result_count = len(items)
    status = "succeeded" if result_count > 0 else "empty"

    summary_items = []
    for item in items[:50]:
        if isinstance(item, dict):
            summary_items.append(
                {
                    "url": item.get("url"),
                    "title": item.get("title"),
                    "description": item.get("description"),
                }
            )
        elif isinstance(item, str):
            summary_items.append({"url": item})

    payload_summary = {
        "result_count": result_count,
        "sample_candidates": summary_items,
    }

    return (status, result_count, payload_summary, None)
