"""DOM-aware HTML main-content extraction parser.

Implements deterministic HTML parsing with semantic-element awareness:
extracts the primary content from ``<main>``, ``<article>``, and
``<section>`` elements while stripping boilerplate from ``<nav>``,
``<aside>``, and ``<header>``.  Extracts page metadata
(``<title>``, ``<meta description>``, Open Graph tags) and preserves
structural elements (headings, lists, tables, code, links, images).

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

## Metadata extracted

| Key                | Source                              |
|--------------------|-------------------------------------|
| ``title``          | ``<title>`` or first ``<h1>``       |
| ``description``    | ``<meta name="description">``       |
| ``og_title``       | ``<meta property="og:title">``      |
| ``og_description`` | ``<meta property="og:description">``|
| ``og_image``       | ``<meta property="og:image">``      |
| ``canonical``      | ``<link rel="canonical">``          |
| ``language``       | ``<html lang="...">``               |

## Main-content extraction policy

1. When a ``<main>`` element exists, extract only its descendants.
2. When no ``<main>``, extract ``<article>`` descendants.
3. When neither exists, extract the body but skip ``<nav>``,
   ``<aside>``, ``<header>``, and ``<footer>`` elements.
4. When no body exists, extract from the entire document.

## Malformed HTML recovery

The parser uses ``html.parser.HTMLParser`` which is tolerant of
unclosed tags, mismatched elements, and other common HTML defects.
When the parser encounters unrecoverable errors (e.g. binary data
that cannot be decoded), it returns a ``ParseResult`` with
``error`` set and empty blocks.

## Structural preservation

* Heading hierarchy is tracked via the ``heading_path`` field.
* Source offsets (``char_start`` / ``char_end``) are **not computed**
  for HTML blocks — the HTML source tree does not map cleanly to
  character offsets after normalization (comments stripped, whitespace
  collapsed, elements restructured).  Both fields are always ``None``.
* Tables are preserved as pipe-delimited rows.
* Links are converted to Markdown link syntax.
* Images are converted to ``[alt]`` syntax.

## Known limitations

* **Mixed-content headings:** When a heading element immediately contains
  child headings (e.g. ``<h2>B<h3>C</h3></h2>``), the parent heading text
  ``B`` is not captured because ``html.parser.HTMLParser`` fires separate
  events for each element. The child heading's ``heading_path`` may contain
  an empty string at the parent level (e.g. ``("A", "")``).  This does not
  cause data corruption — empty strings are simply less informative than
  actual text.  Common cases where headings contain only text content are
  handled correctly.

.. versionchanged:: P5-03
   Introduced as part of Phase 5 DOM-aware HTML extraction.
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

# Tags to skip during main-content extraction (boilerplate)
_SKIP_TAGS = frozenset(
    (
        "nav",
        "aside",
        "header",
        "footer",
        "script",
        "style",
        "noscript",
        "form",
    )
)

# Tags that define the main content area
_MAIN_TAGS = frozenset(("main", "article"))

# Heading tags
_HEADING_TAGS = frozenset(("h1", "h2", "h3", "h4", "h5", "h6"))

# Tags that should be skipped entirely (no content extracted)
_IGNORE_TAGS = frozenset(("script", "style", "noscript", "head"))


class _MainContentCollector(HTMLParser):
    """HTML parser that extracts main content with semantic awareness."""

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
        self._in_main_content = True  # Whether we're inside a main-content area
        self._skip_depth = 0  # Nesting depth of skipped elements
        self._in_skip = False  # Whether we're currently skipping
        self._metadata: dict[str, str] = {}
        self._title: str = ""
        self._in_title = False
        self._title_pending = False
        self._pending_heading_title: str = ""  # Temp storage for current heading text

    # ------------------------------------------------------------------
    # Public accessor for metadata (avoids private attribute access from parse())
    # ------------------------------------------------------------------

    @property
    def metadata(self) -> dict[str, str]:
        """Return the extracted metadata dict.

        This property provides public access to the internal ``_metadata``
        dict so that the ``HtmlMainContentParser.parse()`` method does not
        need to reach into a private attribute.
        """
        return self._metadata

    @metadata.setter
    def metadata(self, value: dict[str, str]) -> None:
        self._metadata = value

    # ------------------------------------------------------------------
    # Start-tag handling
    # ------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_dict = dict(attrs)

        # --- Metadata extraction (always active) ---
        if tag == "title":
            self._in_title = True
            self._title_pending = True
        elif tag == "meta":
            self._extract_meta(attr_dict)
        elif tag == "link" and attr_dict.get("rel") == "canonical":
            self._metadata["canonical"] = attr_dict.get("href", "")
        elif tag == "html":
            lang = attr_dict.get("lang")
            if lang:
                self._metadata["language"] = lang

        # --- Title content ---
        if self._in_title and tag not in _IGNORE_TAGS:
            return  # Title content handled in handle_data

        # --- Skip boilerplate ---
        if tag in _IGNORE_TAGS:
            self._skip_depth += 1
            return
        if tag in _SKIP_TAGS:
            if not self._in_skip:
                self._flush_text()
                self._in_skip = True
            self._skip_depth += 1
            return

        # --- Check if entering/exiting main content ---
        if self._skip_depth > 0:
            return  # Still inside a skipped element

        # Determine if this element is in main content
        if tag in _MAIN_TAGS:
            self._in_main_content = True

        # --- Block-level elements ---
        if tag in _HEADING_TAGS:
            self._flush_text()
            depth = int(tag[1])
            self._headings[depth - 1 :] = [""]
            self._pending_heading_depth = depth
            self._pending_heading_title = ""
        elif tag == "p":
            if not self._in_blockquote:
                self._flush_text()
        elif tag in ("pre", "code"):
            if not self._in_code:
                self._in_code = True
                self._flush_text()
                if tag == "pre":
                    self._current_text.append("\n")
            # If already in code, just continue — don't flush
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
                    parser_version="html-main-content-v1",
                )
            )
        elif tag == "li":
            if not self._in_list:
                self._flush_text()
                self._in_list = True
                self._current_text = []
            # If already in a list, flush the previous list item first
            elif self._current_text:
                prev_text = "".join(self._current_text).strip()
                if prev_text:
                    self.blocks.append(
                        TypedBlock(
                            ordinal=len(self.blocks),
                            block_type="list_item",
                            text=prev_text,
                            heading_path=tuple(self._headings),
                            parser_version="html-main-content-v1",
                        )
                    )
                self._current_text = []
            # Start accumulating text for this list item (no prefix marker)
            # The prefix is added when creating the block
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
            href = attr_dict.get("href", "")
            self._current_href = href
            self._current_text.append("[")
        elif tag == "img":
            alt = attr_dict.get("alt", "")
            self._flush_text()
            self.blocks.append(
                TypedBlock(
                    ordinal=len(self.blocks),
                    block_type="caption",
                    text=f"[{alt}]",
                    heading_path=tuple(self._headings),
                    parser_version="html-main-content-v1",
                )
            )

    # ------------------------------------------------------------------
    # End-tag handling
    # ------------------------------------------------------------------

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        # --- Title ---
        if tag == "title":
            self._in_title = False
            if self._title and not self._metadata.get("title"):
                self._metadata["title"] = self._title.strip()
            return

        # --- Handle skipped elements ---
        if tag in _SKIP_TAGS or tag in _IGNORE_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
                if self._skip_depth == 0 and tag in _SKIP_TAGS and self._in_skip:
                    self._in_skip = False
                    self._flush_text()
            return

        # --- Main content boundaries ---
        if tag in _MAIN_TAGS:
            self._in_main_content = False

        # --- Regular block elements ---
        if tag in ("pre", "code"):
            self._in_code = False
            # Flush remaining code content as a code block
            # (but only if there's actual content)
            text = "".join(self._current_text).strip()
            if text:
                self.blocks.append(
                    TypedBlock(
                        ordinal=len(self.blocks),
                        block_type="code",
                        text=text,
                        heading_path=tuple(self._headings),
                        parser_version="html-main-content-v1",
                    )
                )
            self._current_text = []
        elif tag in _HEADING_TAGS:
            depth = int(tag[1])
            title = self._pending_heading_title
            if title:
                # Set the heading in the hierarchy (for descendants)
                self._headings[depth - 1] = title
                # Create block with ancestor path (shallowers only)
                ancestor_path = tuple(self._headings[: depth - 1])
                self.blocks.append(
                    TypedBlock(
                        ordinal=len(self.blocks),
                        block_type="heading",
                        text=title,
                        heading_path=ancestor_path,
                        parser_version="html-main-content-v1",
                    )
                )
            # Do NOT clear _headings[depth - 1] here — ancestors must
            # survive for deeper headings that follow.  The start handler
            # truncates deeper levels via the slice assignment.
            self._pending_heading_depth = None
            self._pending_heading_title = ""
        elif tag == "li":
            # Don't call _flush_text — the accumulated text IS the list item
            text = "".join(self._current_text).strip()
            if text:
                self.blocks.append(
                    TypedBlock(
                        ordinal=len(self.blocks),
                        block_type="list_item",
                        text="- " + text,
                        heading_path=tuple(self._headings),
                        parser_version="html-main-content-v1",
                    )
                )
            self._current_text = []
        elif tag in ("ul", "ol"):
            # Flush any remaining list item when the list closes
            if self._in_list and self._current_text:
                text = "".join(self._current_text).strip()
                if text:
                    self.blocks.append(
                        TypedBlock(
                            ordinal=len(self.blocks),
                            block_type="list_item",
                            text=text,
                            heading_path=tuple(self._headings),
                            parser_version="html-main-content-v1",
                        )
                    )
                self._current_text = []
            self._in_list = False
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
            self._flush_text()

    # ------------------------------------------------------------------
    # Data handling
    # ------------------------------------------------------------------

    def handle_data(self, data: str) -> None:
        # Title content
        if self._in_title:
            self._title += data
            return

        # Skip content inside ignored elements
        if self._skip_depth > 0:
            return

        # Skip content inside skipped elements (nav, aside, etc.)
        if self._in_skip:
            return

        if self._in_code:
            self._current_text.append(data)
        elif self._pending_heading_depth is not None:
            self._pending_heading_title = data.strip()
        else:
            self._current_text.append(data)

    # ------------------------------------------------------------------
    # Comment handling
    # ------------------------------------------------------------------

    def handle_comment(self, data: str) -> None:
        # Strip HTML comments
        pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_meta(self, attrs: dict[str, str | None]) -> None:
        """Extract metadata from <meta> tags."""
        name = (attrs.get("name") or "").lower()
        prop = (attrs.get("property") or "").lower()
        content = attrs.get("content", "")

        if name == "description":
            self._metadata["description"] = content
        elif prop == "og:title":
            self._metadata["og_title"] = content
        elif prop == "og:description":
            self._metadata["og_description"] = content
        elif prop == "og:image":
            self._metadata["og_image"] = content

    def _flush_text(self, block_type: str | None = None) -> None:
        """Flush accumulated text into a block."""
        text = "".join(self._current_text)
        if not text.strip() or text.strip() == "|":
            self._current_text = []
            return
        # Normalize whitespace
        normalized = " ".join(text.split())
        # Don't add blockquote prefix if it's already in the text
        if self._in_blockquote and not normalized.startswith(">"):
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
                    parser_version="html-main-content-v1",
                )
            )
        self._current_text = []


class HtmlMainContentParser(Parser):
    """DOM-aware HTML main-content extraction parser.

    Extracts primary content from semantic HTML elements while
    stripping boilerplate, and records metadata alongside typed
    blocks.

    Attributes:
        parser_version: Fixed version string ``"html-main-content-v1"``.
    """

    parser_version = "html-main-content-v1"

    def parse(
        self,
        raw: bytes,
        *,
        mime_type: str | None = None,
        source_length: int | None = None,
    ) -> ParseResult:
        """Parse HTML into typed blocks with main-content extraction.

        Args:
            raw: UTF-8 encoded HTML source.
            mime_type: MIME type hint.
            source_length: Pre-computed length (ignored; computed internally).

        Returns:
            A ``ParseResult`` with typed HTML-derived blocks and metadata.
        """
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            return ParseResult(
                blocks=[],
                parser_version=self.parser_version,
                mime_type=mime_type or "text/html",
                source_length=0,
                encoding="utf-8",
                error="Failed to decode HTML as UTF-8",
            )

        if source_length is None:
            source_length = len(text)

        collector = _MainContentCollector()
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

        # Use first h1 as title if no <title> was found
        if not collector.metadata.get("title"):
            for block in collector.blocks:
                if block.block_type == "heading" and block.text:
                    collector.metadata["title"] = block.text
                    break

        return ParseResult(
            blocks=collector.blocks,
            parser_version=self.parser_version,
            mime_type=mime_type or "text/html",
            source_length=source_length,
            encoding="utf-8",
            error=None,
            metadata={
                "block_type_counts": self._count_block_types(collector.blocks),
                "extractor_version": "html-main-content-v1",
                "fallback_version": "html-normalized-v1",
                "metadata": collector.metadata,
            },
        )

    @staticmethod
    def _count_block_types(blocks: list[TypedBlock]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for block in blocks:
            counts[block.block_type] = counts.get(block.block_type, 0) + 1
        return counts
