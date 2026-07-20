from __future__ import annotations

import hashlib
import re

from .domain import Block, Chunk


_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_LIST = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")
_QUOTE = re.compile(r"^\s*>\s?")


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
