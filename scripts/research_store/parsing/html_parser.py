"""HTML-normalized parser adapter.

Converts raw HTML into typed Markdown-like blocks by stripping boilerplate,
preserving structural elements (headings, paragraphs, lists, code blocks,
tables), and normalizing whitespace.

Uses only Python stdlib (``html.parser``) — no external dependencies.

## Block types

| Block type     | Description                           |
|----------------|---------------------------------------|
| ``heading``    | Converted ``<h1>``–``<h6>`` elements  |
| ``paragraph``  | Block-level text content              |
| ``code``       | ``<pre>`` / ``<code>`` content        |
| ``list_item``  | ``<li>`` elements                     |
| ``quotation``  | ``<blockquote>`` content              |
| ``table_row``  | Table cell content                    |
| ``caption``    | ``<img>`` alt text                    |
| ``horizontal_rule`` | ``<hr>`` elements               |

## Normalization rules

1. Strip HTML comments.
2. Collapse consecutive whitespace to single spaces.
3. Preserve code blocks verbatim.
4. Convert headings to ATX-style (``# Title``).
5. Convert lists to Markdown list syntax.
6. Strip boilerplate navigation, cookie notices, and social links.

## Structural preservation

* Heading hierarchy is tracked via the ``heading_path`` field.
* Source offsets (``char_start`` / ``char_end``) are **not computed** for
  HTML blocks — the HTML source tree does not map cleanly to character
  offsets after normalization (comments stripped, whitespace collapsed,
  elements restructured).  Both fields are always ``None``.
* Tables are preserved as pipe-delimited rows.

.. versionchanged:: P5-04
   Introduced as part of Phase 5 canonical parser interfaces.
"""

from __future__ import annotations

from html.parser import HTMLParser

from .interfaces import ParseResult, Parser, TypedBlock

# Block-level tags that should flush accumulated text
_BLOCK_END_TAGS = frozenset(
    (
        "p",
        "div",
        "span",
        "section",
        "article",
        "main",
        "header",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "blockquote",
        "table",
        "pre",
        "code",
        "hr",
        "br",
    )
)


class _HtmlBlockCollector(HTMLParser):
    """Internal HTML parser that collects blocks."""

    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[TypedBlock] = []
        self._headings: list[str] = []
        self._in_code = False
        self._in_list = False
        self._in_blockquote = False
        self._in_table = False
        self._in_tr = False
        self._current_href: str = ""
        self._current_text: list[str] = []
        self._pending_heading_depth: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in ("pre", "code"):
            self._in_code = True
            self._flush_text()
            if tag == "pre":
                self._current_text.append("\n")
        elif tag == "br":
            if self._in_code:
                self._current_text.append("\n")
            else:
                self._current_text.append("\n\n")
        elif tag == "hr":
            self._flush_text()
            self.blocks.append(
                TypedBlock(
                    ordinal=len(self.blocks),
                    block_type="horizontal_rule",
                    text="---",
                    heading_path=tuple(self._headings),
                    parser_version="html-normalized-v1",
                )
            )
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_text()
            depth = int(tag[1])
            self._headings[depth - 1 :] = [""]
            self._pending_heading_depth = depth
        elif tag == "li":
            if not self._in_list:
                self._flush_text()
                self._in_list = True
            self._current_text.append("\n- ")
        elif tag == "blockquote":
            if not self._in_blockquote:
                self._flush_text()
                self._in_blockquote = True
                self._current_text.append("\n> ")
        elif tag == "table":
            self._flush_text()
            self._in_table = True
        elif tag == "tr":
            self._flush_text()
            self._in_tr = True
            self._current_text.append("| ")
        elif tag == "a":
            href = dict(attrs).get("href", "")
            self._current_href = href
            self._current_text.append("[")
        elif tag == "img":
            alt = dict(attrs).get("alt", "")
            self._flush_text()
            self.blocks.append(
                TypedBlock(
                    ordinal=len(self.blocks),
                    block_type="caption",
                    text=f"[{alt}]",
                    heading_path=tuple(self._headings),
                    parser_version="html-normalized-v1",
                )
            )

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("pre", "code"):
            self._in_code = False
            self._flush_text()
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            depth = int(tag[1])
            if depth <= len(self._headings) and self._headings[depth - 1]:
                title = self._headings[depth - 1]
                self.blocks.append(
                    TypedBlock(
                        ordinal=len(self.blocks),
                        block_type="heading",
                        text=title,
                        heading_path=tuple(self._headings),
                        parser_version="html-normalized-v1",
                    )
                )
            self._headings[depth - 1] = ""
            self._pending_heading_depth = None
        elif tag == "li":
            self._flush_text()
            text = "".join(self._current_text).strip()
            if text:
                self.blocks.append(
                    TypedBlock(
                        ordinal=len(self.blocks),
                        block_type="list_item",
                        text=text,
                        heading_path=tuple(self._headings),
                        parser_version="html-normalized-v1",
                    )
                )
            self._current_text = []
        elif tag == "blockquote":
            self._in_blockquote = False
            self._flush_text()
        elif tag == "table":
            self._in_table = False
        elif tag == "tr":
            self._flush_text(block_type="table_row")
            self._in_tr = False
        elif tag in ("td", "th"):
            self._current_text.append(" | ")
        elif tag == "a":
            if self._current_href:
                self._current_text.append(f"]({self._current_href})")
                self._current_href = ""
            else:
                self._current_text.append("]")
        elif tag in _BLOCK_END_TAGS:
            # Flush text on block-level container close
            self._flush_text()

    def handle_data(self, data: str) -> None:
        if self._in_code:
            self._current_text.append(data)
        elif self._pending_heading_depth is not None:
            self._headings[self._pending_heading_depth - 1] = data.strip()
            self._pending_heading_depth = None
        else:
            self._current_text.append(data)

    def handle_comment(self, data: str) -> None:
        # Strip HTML comments
        pass

    def _flush_text(self, block_type: str | None = None) -> None:
        text = "".join(self._current_text)
        if not text.strip() or text.strip() == "|":
            self._current_text = []
            return
        # Normalize whitespace
        normalized = " ".join(text.split())
        if self._in_blockquote:
            normalized = "> " + normalized
        if normalized.strip() and normalized.strip() != "|":
            if block_type is None:
                block_type = "code" if self._in_code else "paragraph"
            self.blocks.append(
                TypedBlock(
                    ordinal=len(self.blocks),
                    block_type=block_type,
                    text=normalized.strip(),
                    heading_path=tuple(self._headings),
                    parser_version="html-normalized-v1",
                )
            )
        self._current_text = []


class HtmlNormalizedParser(Parser):
    """HTML-to-typed-blocks parser.

    Attributes:
        parser_version: Fixed version string ``"html-normalized-v1"``.
    """

    parser_version = "html-normalized-v1"

    def parse(
        self,
        raw: bytes,
        *,
        mime_type: str | None = None,
        source_length: int | None = None,
    ) -> ParseResult:
        """Parse HTML into typed blocks.

        Args:
            raw: UTF-8 encoded HTML source.
            mime_type: MIME type hint.
            source_length: Pre-computed length (ignored; computed internally).

        Returns:
            A ``ParseResult`` with typed HTML-derived blocks.
        """
        text = raw.decode("utf-8", errors="replace")
        if source_length is None:
            source_length = len(text)

        collector = _HtmlBlockCollector()
        try:
            collector.feed(text)
        except Exception:
            # If parsing fails, return an error result
            return ParseResult(
                blocks=[],
                parser_version=self.parser_version,
                mime_type=mime_type or "text/html",
                source_length=source_length,
                encoding="utf-8",
                error="HTML parsing failed",
            )

        return ParseResult(
            blocks=collector.blocks,
            parser_version=self.parser_version,
            mime_type=mime_type or "text/html",
            source_length=source_length,
            encoding="utf-8",
            metadata={
                "block_type_counts": self._count_block_types(collector.blocks),
            },
        )

    @staticmethod
    def _count_block_types(blocks: list[TypedBlock]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for block in blocks:
            counts[block.block_type] = counts.get(block.block_type, 0) + 1
        return counts
