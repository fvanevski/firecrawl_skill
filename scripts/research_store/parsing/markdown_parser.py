"""Markdown parser adapter.

Parses Markdown source into typed blocks preserving structural elements
(headings, paragraphs, code fences, lists, blockquotes, tables, captions).

This adapter refactors the logic from the legacy ``structural_blocks()``
function in ``research_store.parsing`` while adding typed block output,
parser-version tracking, and explicit offset recording.

## Block types

| Block type     | Description                           |
|----------------|---------------------------------------|
| ``heading``    | ATX-style headings (``#`` to ``######``) |
| ``paragraph``  | Run-of text lines grouped together    |
| ``code``       | Content inside fenced code blocks     |
| ``list_item``  | Ordered or unordered list items       |
| ``quotation``  | Blockquote lines (``>`` prefix)       |
| ``table_row``  | Pipe-delimited table rows             |
| ``caption``    | Image reference lines (``![...]``)    |
| ``blank``      | Empty separator lines (metadata only) |

.. versionchanged:: P5-04
   Introduced as part of Phase 5 canonical parser interfaces.
"""

from __future__ import annotations

import re

from .interfaces import ParseResult, Parser, TypedBlock


_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_LIST = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")
_QUOTE = re.compile(r"^\s*>\s?")
_IMAGE_REF = re.compile(r"^!\[[^\]]*\]\(")


class MarkdownParser(Parser):
    """Markdown structural parser.

    Attributes:
        parser_version: Fixed version string ``"markdown-v1"``.
    """

    parser_version = "markdown-v1"

    def parse(
        self,
        raw: bytes,
        *,
        mime_type: str | None = None,
        source_length: int | None = None,
    ) -> ParseResult:
        """Parse Markdown source into typed blocks.

        Args:
            raw: UTF-8 encoded Markdown source.
            mime_type: MIME type hint (ignored; always ``text/markdown``).
            source_length: Pre-computed length (ignored; computed internally).

        Returns:
            A ``ParseResult`` with typed Markdown blocks.
        """
        text = raw.decode("utf-8", errors="replace")
        if source_length is None:
            source_length = len(text)

        blocks = self._structural_blocks(text)
        return ParseResult(
            blocks=blocks,
            parser_version=self.parser_version,
            mime_type=mime_type or "text/markdown",
            source_length=source_length,
            encoding="utf-8",
            metadata={
                "block_type_counts": self._count_block_types(blocks),
            },
        )

    # ------------------------------------------------------------------
    # Internal parsing logic (refactored from legacy structural_blocks)
    # ------------------------------------------------------------------

    def _structural_blocks(self, markdown: str) -> list[TypedBlock]:
        """Parse Markdown into typed blocks preserving offsets."""
        blocks: list[TypedBlock] = []
        headings: list[str] = []
        offset = 0
        paragraph: list[tuple[str, int, int]] = []
        code: list[tuple[str, int, int]] = []
        in_code = False

        def emit(lines: list[tuple[str, int, int]], block_type: str) -> None:
            if not lines:
                return
            text = "".join(item[0] for item in lines).strip("\n")
            if text or block_type == "code":
                blocks.append(
                    TypedBlock(
                        ordinal=len(blocks),
                        block_type=block_type,
                        text=text,
                        heading_path=tuple(headings),
                        char_start=lines[0][1],
                        char_end=lines[-1][2],
                        parser_version=self.parser_version,
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
                    TypedBlock(
                        ordinal=len(blocks),
                        block_type="heading",
                        text=title,
                        heading_path=tuple(headings),
                        char_start=start,
                        char_end=end,
                        parser_version=self.parser_version,
                    )
                )
            elif not line.strip():
                emit(paragraph, "paragraph")
            elif _LIST.match(line):
                emit(paragraph, "paragraph")
                blocks.append(
                    TypedBlock(
                        ordinal=len(blocks),
                        block_type="list_item",
                        text=_LIST.sub("", line).strip(),
                        heading_path=tuple(headings),
                        char_start=start,
                        char_end=end,
                        parser_version=self.parser_version,
                    )
                )
            elif _QUOTE.match(line):
                emit(paragraph, "paragraph")
                blocks.append(
                    TypedBlock(
                        ordinal=len(blocks),
                        block_type="quotation",
                        text=_QUOTE.sub("", line).strip(),
                        heading_path=tuple(headings),
                        char_start=start,
                        char_end=end,
                        parser_version=self.parser_version,
                    )
                )
            elif "|" in line and line.count("|") >= 2:
                emit(paragraph, "paragraph")
                blocks.append(
                    TypedBlock(
                        ordinal=len(blocks),
                        block_type="table_row",
                        text=line.strip(),
                        heading_path=tuple(headings),
                        char_start=start,
                        char_end=end,
                        parser_version=self.parser_version,
                    )
                )
            elif _IMAGE_REF.match(line):
                emit(paragraph, "paragraph")
                blocks.append(
                    TypedBlock(
                        ordinal=len(blocks),
                        block_type="caption",
                        text=line.strip(),
                        heading_path=tuple(headings),
                        char_start=start,
                        char_end=end,
                        parser_version=self.parser_version,
                    )
                )
            else:
                paragraph.append((line, start, end))
        emit(code, "code")
        emit(paragraph, "paragraph")
        return blocks

    @staticmethod
    def _count_block_types(blocks: list[TypedBlock]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for block in blocks:
            counts[block.block_type] = counts.get(block.block_type, 0) + 1
        return counts
