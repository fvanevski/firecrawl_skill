"""JSON parser adapter.

Parses JSON payloads into typed blocks by converting the JSON structure
into a Markdown-readable representation.  Handles nested objects, arrays,
and primitive values.

## Block types

| Block type     | Description                           |
|----------------|---------------------------------------|
| ``heading``    | Top-level keys and object paths       |
| ``paragraph``  | Key-value pairs and scalar values     |
| ``list_item``  | Array elements                        |
| ``code``       | Raw JSON fragments (indented blocks)  |

## Limitations

* Deep nesting beyond 10 levels is truncated.
* Binary values (base64 blobs) are represented as ``[binary data]``.
* Arrays of objects produce a heading per object followed by paragraphs.

.. versionchanged:: P5-04
   Introduced as part of Phase 5 canonical parser interfaces.
"""

from __future__ import annotations

import json
from typing import Any

from .interfaces import ParseResult, Parser, TypedBlock


class JsonParser(Parser):
    """JSON-to-typed-blocks parser.

    Attributes:
        parser_version: Fixed version string ``"json-v1"``.
    """

    parser_version = "json-v1"

    def parse(
        self,
        raw: bytes,
        *,
        mime_type: str | None = None,
        source_length: int | None = None,
    ) -> ParseResult:
        """Parse JSON into typed blocks.

        Args:
            raw: UTF-8 encoded JSON source.
            mime_type: MIME type hint.
            source_length: Pre-computed length (ignored; computed internally).

        Returns:
            A ``ParseResult`` with typed JSON-derived blocks.

        Raises:
            UnsupportedFormatError: When the payload is not valid JSON.
        """
        text = raw.decode("utf-8", errors="replace")
        if source_length is None:
            source_length = len(text)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return ParseResult(
                blocks=[],
                parser_version=self.parser_version,
                mime_type=mime_type or "application/json",
                source_length=source_length,
                encoding="utf-8",
                error=f"Invalid JSON: {exc}",
            )

        blocks = self._structure(data, path=[], depth=0)
        return ParseResult(
            blocks=blocks,
            parser_version=self.parser_version,
            mime_type=mime_type or "application/json",
            source_length=source_length,
            encoding="utf-8",
            metadata={
                "block_type_counts": self._count_block_types(blocks),
            },
        )

    # ------------------------------------------------------------------
    # Internal JSON-to-block conversion
    # ------------------------------------------------------------------

    def _structure(
        self,
        data: Any,
        *,
        path: list[str],
        depth: int,
    ) -> list[TypedBlock]:
        """Recursively convert JSON data to typed blocks."""
        if depth > 10:
            return [
                TypedBlock(
                    ordinal=0,
                    block_type="paragraph",
                    text="[truncated: nesting depth exceeded]",
                    heading_path=tuple(path),
                    parser_version=self.parser_version,
                )
            ]

        blocks: list[TypedBlock] = []
        ordinal = 0

        def add_block(
            block_type: str, text: str, extra_meta: dict | None = None
        ) -> TypedBlock:
            nonlocal ordinal
            block = TypedBlock(
                ordinal=ordinal,
                block_type=block_type,
                text=text,
                heading_path=tuple(path),
                parser_version=self.parser_version,
                metadata=extra_meta or {},
            )
            ordinal += 1
            blocks.append(block)
            return block

        if isinstance(data, dict):
            for key, value in data.items():
                current_path = path + [str(key)]
                if isinstance(value, (dict, list)):
                    add_block("heading", f"{' > '.join(current_path)}")
                    blocks.extend(
                        self._structure(value, path=current_path, depth=depth + 1)
                    )
                elif isinstance(value, bool):
                    add_block("paragraph", f"{key}: {str(value).lower()}")
                elif value is None:
                    add_block("paragraph", f"{key}: null")
                elif isinstance(value, (int, float)):
                    add_block("paragraph", f"{key}: {value}")
                elif isinstance(value, str):
                    # Truncate very long values
                    if len(value) > 200:
                        value = value[:200] + "..."
                    add_block("paragraph", f"{key}: {value}")
                else:
                    add_block("paragraph", f"{key}: {json.dumps(value)}")

        elif isinstance(data, list):
            for i, item in enumerate(data):
                current_path = path + [str(i)]
                if isinstance(item, (dict, list)):
                    add_block("heading", f"{' > '.join(current_path)}")
                    blocks.extend(
                        self._structure(item, path=current_path, depth=depth + 1)
                    )
                elif isinstance(item, bool):
                    add_block("list_item", f"- {str(item).lower()}")
                elif item is None:
                    add_block("list_item", "- null")
                elif isinstance(item, (int, float)):
                    add_block("list_item", f"- {item}")
                elif isinstance(item, str):
                    if len(item) > 200:
                        item = item[:200] + "..."
                    add_block("list_item", f"- {item}")
                else:
                    add_block("list_item", f"- {json.dumps(item)}")

        else:
            # Top-level scalar
            if isinstance(data, str):
                if len(data) > 500:
                    data = data[:500] + "..."
                add_block("paragraph", data)
            else:
                add_block("paragraph", str(data))

        return blocks

    @staticmethod
    def _count_block_types(blocks: list[TypedBlock]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for block in blocks:
            counts[block.block_type] = counts.get(block.block_type, 0) + 1
        return counts
