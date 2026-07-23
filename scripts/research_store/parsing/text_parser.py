"""Plain-text parser adapter.

Treats raw input as plain text, splitting on blank lines to produce
paragraph blocks.  No structural interpretation is performed — every
non-empty line group becomes a ``paragraph`` block.

This is the simplest parser and serves as the default fallback when
MIME type detection fails.

## Block types

| Block type     | Description                           |
|----------------|---------------------------------------|
| ``paragraph``  | Consecutive non-blank line groups     |

## Limitations

* No heading detection.
* No list or code block recognition.
* No structural metadata beyond line-group offsets.

.. versionchanged:: P5-04
   Introduced as part of Phase 5 canonical parser interfaces.
"""

from __future__ import annotations


from .interfaces import ParseResult, Parser, TypedBlock


class PlainTextParser(Parser):
    """Plain-text paragraph parser.

    Attributes:
        parser_version: Fixed version string ``"text-v1"``.
    """

    parser_version = "text-v1"

    def parse(
        self,
        raw: bytes,
        *,
        mime_type: str | None = None,
        source_length: int | None = None,
    ) -> ParseResult:
        """Parse plain text into paragraph blocks.

        Args:
            raw: UTF-8 encoded plain text.
            mime_type: MIME type hint.
            source_length: Pre-computed length (ignored; computed internally).

        Returns:
            A ``ParseResult`` with paragraph blocks.
        """
        text = raw.decode("utf-8", errors="replace")
        if source_length is None:
            source_length = len(text)

        blocks = self._paragraphs(text)
        return ParseResult(
            blocks=blocks,
            parser_version=self.parser_version,
            mime_type=mime_type or "text/plain",
            source_length=source_length,
            encoding="utf-8",
            metadata={
                "block_type_counts": self._count_block_types(blocks),
            },
        )

    @staticmethod
    def _paragraphs(text: str) -> list[TypedBlock]:
        """Split text into paragraph blocks on blank-line boundaries."""
        blocks: list[TypedBlock] = []
        current_lines: list[tuple[str, int, int]] = []
        offset = 0

        for line in text.splitlines(keepends=True):
            start = offset
            end = offset + len(line)
            offset = end

            if not line.strip():
                if current_lines:
                    block_text = "\n".join(item[0] for item in current_lines).strip()
                    if block_text:
                        blocks.append(
                            TypedBlock(
                                ordinal=len(blocks),
                                block_type="paragraph",
                                text=block_text,
                                heading_path=(),
                                char_start=current_lines[0][1],
                                char_end=end,
                                parser_version="text-v1",
                            )
                        )
                    current_lines = []
            else:
                current_lines.append((line, start, end))

        # Flush remaining
        if current_lines:
            block_text = "\n".join(item[0] for item in current_lines).strip()
            if block_text:
                blocks.append(
                    TypedBlock(
                        ordinal=len(blocks),
                        block_type="paragraph",
                        text=block_text,
                        heading_path=(),
                        char_start=current_lines[0][1],
                        char_end=current_lines[-1][2],
                        parser_version="text-v1",
                    )
                )

        return blocks

    @staticmethod
    def _count_block_types(blocks: list[TypedBlock]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for block in blocks:
            counts[block.block_type] = counts.get(block.block_type, 0) + 1
        return counts
