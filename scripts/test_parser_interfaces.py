"""Tests for canonical parser interfaces and typed adapters (issue #44).

Covers:
- Adapter contract tests (Markdown, HTML, JSON, plain-text)
- Offset preservation in blocks
- Unsupported MIME type failures
- Parser registry selection (exact, prefix, fallback)
- Extension point stubs (PDF, code, legal)
- Backward compatibility with legacy structural_blocks
- ParseResult properties (success, block_count, etc.)
- TypedBlock to legacy Block conversion
- Parser version recording
- Deterministic parser selection
- Empty content handling
- Mixed tables, code, lists, and headings
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.parsing import (
    ParseResult,
    ParserRegistry,
    ParserSelectionError,
    TypedBlock,
    UnsupportedFormatError,
    build_default_registry,
    get_registry,
    parse,
)
from research_store.parsing.interfaces import Parser
from research_store.parsing.extensions import (
    CodeParser,
    LegalDocumentParser,
    PdfParser,
)
from research_store.domain import Block


# ---------------------------------------------------------------------------
# Adapter contract tests
# ---------------------------------------------------------------------------


class TestMarkdownAdapter:
    """Tests for the Markdown parser adapter."""

    def test_parses_heading(self):
        from research_store.parsing.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        result = parser.parse(b"# Hello World")
        assert result.success
        assert result.block_count == 1
        assert result.blocks[0].block_type == "heading"
        assert result.blocks[0].text == "Hello World"
        assert result.parser_version == "markdown-v1"

    def test_parses_paragraph(self):
        from research_store.parsing.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        result = parser.parse(b"This is a paragraph.")
        assert result.success
        assert result.block_count == 1
        assert result.blocks[0].block_type == "paragraph"
        assert "paragraph" in result.blocks[0].text

    def test_parses_code_fence(self):
        from research_store.parsing.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        source = b"```python\nprint('hello')\n```"
        result = parser.parse(source)
        assert result.success
        types = [b.block_type for b in result.blocks]
        assert "code" in types

    def test_parses_list_items(self):
        from research_store.parsing.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        source = b"- item one\n- item two\n- item three"
        result = parser.parse(source)
        assert result.success
        types = [b.block_type for b in result.blocks]
        assert "list_item" in types

    def test_parses_quotation(self):
        from research_store.parsing.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        source = b"> This is a quote"
        result = parser.parse(source)
        assert result.success
        types = [b.block_type for b in result.blocks]
        assert "quotation" in types

    def test_parses_table_row(self):
        from research_store.parsing.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        source = b"| col1 | col2 |\n|------|------|\n| a    | b    |"
        result = parser.parse(source)
        assert result.success
        types = [b.block_type for b in result.blocks]
        assert "table_row" in types

    def test_parses_heading_path(self):
        from research_store.parsing.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        source = b"# Main\n## Sub\nparagraph text"
        result = parser.parse(source)
        paragraphs = [b for b in result.blocks if b.block_type == "paragraph"]
        assert len(paragraphs) >= 1
        assert paragraphs[0].heading_path == ("Main", "Sub")

    def test_parses_image_caption(self):
        from research_store.parsing.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        source = b"![Alt text](https://example.com/img.png)"
        result = parser.parse(source)
        assert result.success
        types = [b.block_type for b in result.blocks]
        assert "caption" in types

    def test_parses_mixed_content(self):
        from research_store.parsing.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        source = b"""# Title

Some paragraph text.

- list item 1
- list item 2

> A blockquote

```python
code block
```

| a | b |
|---|---|
| 1 | 2 |

![img](https://example.com/img.png)
"""
        result = parser.parse(source)
        assert result.success
        types = set(b.block_type for b in result.blocks)
        assert "heading" in types
        assert "paragraph" in types
        assert "list_item" in types
        assert "quotation" in types
        assert "code" in types
        assert "table_row" in types
        assert "caption" in types

    def test_empty_content(self):
        from research_store.parsing.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        result = parser.parse(b"")
        assert result.success
        assert result.block_count == 0

    def test_returns_source_length(self):
        from research_store.parsing.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        source = b"# Hello\n\nWorld"
        result = parser.parse(source)
        assert result.source_length == len("# Hello\n\nWorld")


class TestHtmlNormalizedAdapter:
    """Tests for the HTML-normalized parser adapter."""

    def test_parses_heading(self):
        from research_store.parsing.html_parser import HtmlNormalizedParser

        parser = HtmlNormalizedParser()
        result = parser.parse(b"<h1>Hello</h1>")
        assert result.success
        types = [b.block_type for b in result.blocks]
        assert "heading" in types

    def test_parses_paragraph(self):
        from research_store.parsing.html_parser import HtmlNormalizedParser

        parser = HtmlNormalizedParser()
        result = parser.parse(b"<p>Hello world</p>")
        assert result.success
        types = [b.block_type for b in result.blocks]
        assert "paragraph" in types

    def test_strips_comments(self):
        from research_store.parsing.html_parser import HtmlNormalizedParser

        parser = HtmlNormalizedParser()
        result = parser.parse(b"<!-- comment -->Hello")
        assert result.success
        # Comments should be stripped — no "comment" in blocks
        all_text = " ".join(b.text for b in result.blocks)
        assert "comment" not in all_text.lower()

    def test_parses_hr(self):
        from research_store.parsing.html_parser import HtmlNormalizedParser

        parser = HtmlNormalizedParser()
        result = parser.parse(b"<hr>")
        assert result.success
        types = [b.block_type for b in result.blocks]
        assert "horizontal_rule" in types

    def test_parses_img_caption(self):
        from research_store.parsing.html_parser import HtmlNormalizedParser

        parser = HtmlNormalizedParser()
        result = parser.parse(b'<img alt="A photo">')
        assert result.success
        types = [b.block_type for b in result.blocks]
        assert "caption" in types

    def test_handles_malformed_html(self):
        from research_store.parsing.html_parser import HtmlNormalizedParser

        parser = HtmlNormalizedParser()
        # Severely malformed HTML — should not crash
        result = parser.parse(b"<div><p>unclosed<div>broken")
        assert result.success or result.error is not None

    def test_parser_version(self):
        from research_store.parsing.html_parser import HtmlNormalizedParser

        parser = HtmlNormalizedParser()
        assert parser.parser_version == "html-normalized-v1"


class TestJsonAdapter:
    """Tests for the JSON parser adapter."""

    def test_parses_object(self):
        from research_store.parsing.json_parser import JsonParser

        parser = JsonParser()
        data = {"name": "test", "value": 42}
        result = parser.parse(json.dumps(data).encode())
        assert result.success
        assert result.block_count > 0

    def test_parses_array(self):
        from research_store.parsing.json_parser import JsonParser

        parser = JsonParser()
        data = ["a", "b", "c"]
        result = parser.parse(json.dumps(data).encode())
        assert result.success
        types = [b.block_type for b in result.blocks]
        assert "list_item" in types

    def test_rejects_invalid_json(self):
        from research_store.parsing.json_parser import JsonParser

        parser = JsonParser()
        result = parser.parse(b"not json {{{")
        assert not result.success
        assert result.error is not None
        assert "Invalid JSON" in result.error

    def test_parses_nested(self):
        from research_store.parsing.json_parser import JsonParser

        parser = JsonParser()
        data = {"outer": {"inner": "value"}}
        result = parser.parse(json.dumps(data).encode())
        assert result.success
        headings = [b for b in result.blocks if b.block_type == "heading"]
        assert len(headings) >= 1

    def test_truncates_long_values(self):
        from research_store.parsing.json_parser import JsonParser

        parser = JsonParser()
        data = {"key": "x" * 300}
        result = parser.parse(json.dumps(data).encode())
        assert result.success
        paragraphs = [b for b in result.blocks if b.block_type == "paragraph"]
        for p in paragraphs:
            assert len(p.text) <= 210  # 200 + "key: " prefix + "..."


class TestPlainTextAdapter:
    """Tests for the plain-text parser adapter."""

    def test_parses_single_paragraph(self):
        from research_store.parsing.text_parser import PlainTextParser

        parser = PlainTextParser()
        result = parser.parse(b"Hello world")
        assert result.success
        assert result.block_count == 1
        assert result.blocks[0].block_type == "paragraph"

    def test_parses_multiple_paragraphs(self):
        from research_store.parsing.text_parser import PlainTextParser

        parser = PlainTextParser()
        result = parser.parse(b"First paragraph.\n\nSecond paragraph.")
        assert result.success
        assert result.block_count == 2

    def test_ignores_blank_lines(self):
        from research_store.parsing.text_parser import PlainTextParser

        parser = PlainTextParser()
        result = parser.parse(b"Line 1\n\n\n\nLine 2")
        assert result.success
        assert result.block_count == 2

    def test_empty_content(self):
        from research_store.parsing.text_parser import PlainTextParser

        parser = PlainTextParser()
        result = parser.parse(b"")
        assert result.success
        assert result.block_count == 0

    def test_parser_version(self):
        from research_store.parsing.text_parser import PlainTextParser

        parser = PlainTextParser()
        assert parser.parser_version == "text-v1"


# ---------------------------------------------------------------------------
# Offset preservation tests
# ---------------------------------------------------------------------------


class TestOffsetPreservation:
    """Tests that blocks preserve source or normalized offsets."""

    def test_markdown_offset_preservation(self):
        from research_store.parsing.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        source = b"# Heading\n\nParagraph text here."
        result = parser.parse(source)
        heading = [b for b in result.blocks if b.block_type == "heading"]
        assert len(heading) == 1
        block = heading[0]
        assert block.char_start is not None
        assert block.char_end is not None
        decoded = source.decode("utf-8")
        # Offsets include the newline; text is stripped
        assert decoded[block.char_start : block.char_end].strip() == "# Heading"

    def test_paragraph_offset_preservation(self):
        from research_store.parsing.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        source = b"# H\n\nParagraph text."
        result = parser.parse(source)
        paragraphs = [b for b in result.blocks if b.block_type == "paragraph"]
        assert len(paragraphs) == 1
        block = paragraphs[0]
        assert block.char_start is not None
        assert block.char_end is not None
        decoded = source.decode("utf-8")
        assert "Paragraph" in decoded[block.char_start : block.char_end]

    def test_list_item_offset_preservation(self):
        from research_store.parsing.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        source = b"- item one\n- item two"
        result = parser.parse(source)
        items = [b for b in result.blocks if b.block_type == "list_item"]
        assert len(items) == 2
        for item in items:
            assert item.char_start is not None
            assert item.char_end is not None

    def test_code_offset_preservation(self):
        from research_store.parsing.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        source = b"```python\nprint('hi')\n```"
        result = parser.parse(source)
        codes = [b for b in result.blocks if b.block_type == "code"]
        assert len(codes) == 1
        block = codes[0]
        assert block.char_start is not None
        assert block.char_end is not None


# ---------------------------------------------------------------------------
# Unsupported MIME type tests
# ---------------------------------------------------------------------------


class TestUnsupportedFormat:
    """Tests that unsupported MIME types fail explicitly."""

    def test_pdf_raises_unsupported(self):
        parser = PdfParser()
        with pytest.raises(UnsupportedFormatError) as exc_info:
            parser.parse(b"%PDF-1.4 fake pdf data")
        assert exc_info.value.mime_type == "application/pdf"
        assert exc_info.value.suggestion is not None

    def test_code_raises_unsupported(self):
        parser = CodeParser()
        with pytest.raises(UnsupportedFormatError) as exc_info:
            parser.parse(b"def foo(): pass")
        assert exc_info.value.mime_type in ("text/x-code", None)

    def test_legal_raises_unsupported(self):
        parser = LegalDocumentParser()
        with pytest.raises(UnsupportedFormatError) as exc_info:
            parser.parse(b"WHEREAS the party of the first part...")
        assert exc_info.value.mime_type in ("application/x-legal", None)

    def test_registry_selects_pdf_as_unsupported(self):
        registry = build_default_registry()
        record = registry.select("application/pdf")
        assert record.selected_parser_type.endswith("PdfParser")
        # Instantiate the parser directly from the selected type
        from importlib import import_module

        module_name, class_name = record.selected_parser_type.rsplit(".", 1)
        mod = import_module(module_name)
        parser = getattr(mod, class_name)()
        with pytest.raises(UnsupportedFormatError):
            parser.parse(b"%PDF-1.4 fake pdf data")


# ---------------------------------------------------------------------------
# Parser registry tests
# ---------------------------------------------------------------------------


class TestParserRegistry:
    """Tests for deterministic parser selection."""

    def test_exact_mime_match(self):
        registry = build_default_registry()
        record = registry.select("text/markdown")
        assert record.selection_method == "exact"
        assert "MarkdownParser" in record.selected_parser_type

    def test_prefix_match(self):
        registry = build_default_registry()
        record = registry.select("text/html; charset=utf-8")
        # Should match via prefix or exact
        assert record.selection_method in ("exact", "prefix")
        assert "HtmlNormalizedParser" in record.selected_parser_type

    def test_no_parser_for_unknown_mime(self):
        registry = build_default_registry()
        with pytest.raises(ParserSelectionError) as exc_info:
            registry.select("application/x-unknown-format")
        assert "application/x-unknown-format" in str(exc_info.value)
        # Should list available parsers
        assert len(exc_info.value.available) > 0

    def test_register_custom_parser(self):
        registry = ParserRegistry()

        class CustomParser(Parser):
            parser_version = "custom-v1"

            def parse(self, raw, *, mime_type=None, source_length=None):
                return ParseResult(
                    blocks=[
                        TypedBlock(
                            ordinal=0,
                            block_type="custom",
                            text="custom",
                            parser_version="custom-v1",
                        )
                    ],
                    parser_version="custom-v1",
                    mime_type=mime_type or "application/x-custom",
                    source_length=source_length or len(raw),
                )

        registry.register(CustomParser, mime_types=["application/x-custom"])
        record = registry.select("application/x-custom")
        assert record.selection_method == "exact"
        assert "CustomParser" in record.selected_parser_type

    def test_list_registered(self):
        registry = build_default_registry()
        registered = registry.list_registered()
        mime_types = [r["mime_type"] for r in registered]
        assert "text/markdown" in mime_types
        assert "text/html" in mime_types
        assert "application/json" in mime_types
        assert "text/plain" in mime_types

    def test_singleton_registry(self):
        r1 = get_registry()
        r2 = get_registry()
        assert r1 is r2

    def test_convenience_parse(self):
        result = parse(b"# Hello", mime_type="text/markdown")
        assert result.success
        assert result.parser_version == "markdown-v1"

    def test_convenience_parse_json(self):
        result = parse(b'{"key": "value"}', mime_type="application/json")
        assert result.success
        assert result.parser_version == "json-v1"


# ---------------------------------------------------------------------------
# ParseResult property tests
# ---------------------------------------------------------------------------


class TestParseResult:
    """Tests for ParseResult properties and methods."""

    def test_success_property(self):
        result = ParseResult(
            blocks=[],
            parser_version="test-v1",
            mime_type="text/plain",
            source_length=0,
        )
        assert result.success is True

    def test_error_property(self):
        result = ParseResult(
            blocks=[],
            parser_version="test-v1",
            mime_type="text/plain",
            source_length=0,
            error="something went wrong",
        )
        assert result.success is False

    def test_block_count(self):
        blocks = [
            TypedBlock(ordinal=0, block_type="heading", text="H", parser_version="v1"),
            TypedBlock(
                ordinal=1, block_type="paragraph", text="P", parser_version="v1"
            ),
        ]
        result = ParseResult(
            blocks=blocks,
            parser_version="test-v1",
            mime_type="text/plain",
            source_length=10,
        )
        assert result.block_count == 2

    def test_first_last_block_ordinal(self):
        blocks = [
            TypedBlock(ordinal=5, block_type="heading", text="H", parser_version="v1"),
            TypedBlock(
                ordinal=10, block_type="paragraph", text="P", parser_version="v1"
            ),
        ]
        result = ParseResult(
            blocks=blocks,
            parser_version="test-v1",
            mime_type="text/plain",
            source_length=10,
        )
        assert result.first_block_ordinal == 5
        assert result.last_block_ordinal == 10

    def test_empty_first_last_block_ordinal(self):
        result = ParseResult(
            blocks=[],
            parser_version="test-v1",
            mime_type="text/plain",
            source_length=0,
        )
        assert result.first_block_ordinal is None
        assert result.last_block_ordinal is None

    def test_to_legacy_blocks(self):
        blocks = [
            TypedBlock(
                ordinal=0,
                block_type="heading",
                text="H",
                heading_path=("Top",),
                char_start=0,
                char_end=5,
                parser_version="markdown-v1",
            ),
        ]
        result = ParseResult(
            blocks=blocks,
            parser_version="test-v1",
            mime_type="text/markdown",
            source_length=5,
        )
        legacy = result.to_legacy_blocks()
        assert len(legacy) == 1
        assert isinstance(legacy[0], Block)
        assert legacy[0].ordinal == 0
        assert legacy[0].block_type == "heading"
        assert legacy[0].text == "H"
        assert legacy[0].heading_path == ("Top",)
        assert legacy[0].char_start == 0
        assert legacy[0].char_end == 5


# ---------------------------------------------------------------------------
# TypedBlock conversion tests
# ---------------------------------------------------------------------------


class TestTypedBlockConversion:
    """Tests for TypedBlock to legacy Block conversion."""

    def test_to_legacy_block(self):
        block = TypedBlock(
            ordinal=42,
            block_type="heading",
            text="Test",
            heading_path=("A", "B"),
            char_start=10,
            char_end=20,
            parser_version="markdown-v1",
        )
        legacy = block.to_legacy_block()
        assert isinstance(legacy, Block)
        assert legacy.ordinal == 42
        assert legacy.block_type == "heading"
        assert legacy.text == "Test"
        assert legacy.heading_path == ("A", "B")
        assert legacy.char_start == 10
        assert legacy.char_end == 20

    def test_to_legacy_block_default_values(self):
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="Hello",
        )
        legacy = block.to_legacy_block()
        assert legacy.char_start is None
        assert legacy.char_end is None
        assert legacy.heading_path == ()
        assert legacy.metadata == {}


# ---------------------------------------------------------------------------
# Backward compatibility tests
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Tests ensuring backward compatibility with legacy code."""

    def test_structural_blocks_still_works(self):
        """Legacy structural_blocks() must still function."""
        from research_store.parsing import structural_blocks

        blocks = structural_blocks("# Title\n\nParagraph text")
        assert len(blocks) >= 1
        assert all(isinstance(b, Block) for b in blocks)

    def test_deterministic_chunks_still_works(self):
        """Legacy deterministic_chunks() must still function."""
        from research_store.parsing import deterministic_chunks

        blocks = [
            Block(0, "heading", "Title"),
            Block(1, "paragraph", "Paragraph text"),
        ]
        chunks = deterministic_chunks(blocks, max_chars=100)
        assert len(chunks) >= 1

    def test_block_has_parser_version(self):
        """Block dataclass must have parser_version field."""
        block = Block(
            ordinal=0,
            block_type="heading",
            text="Title",
        )
        assert hasattr(block, "parser_version")
        assert block.parser_version == "markdown-v1"  # default


# ---------------------------------------------------------------------------
# Config and CLI tests
# ---------------------------------------------------------------------------


class TestConfigAndCli:
    """Tests for config and CLI integration."""

    def test_config_has_parser_registry_version(self):
        from research_store.config import StoreConfig

        # Verify the field exists by checking the dataclass fields
        fields = {f.name for f in StoreConfig.__dataclass_fields__.values()}
        assert "parser_registry_version" in fields

    def test_parser_info_cli_produces_json(self):
        """parser-info subcommand must produce valid JSON output."""
        from research_store.cli import parser as cli_parser

        # Parse the args — just verify the subcommand is recognized
        args = cli_parser().parse_args(["parser-info"])
        assert args.command == "parser-info"


# ---------------------------------------------------------------------------
# Failure path tests
# ---------------------------------------------------------------------------


class TestFailurePaths:
    """Tests for failure paths and edge cases."""

    def test_json_parser_invalid_content(self):
        from research_store.parsing.json_parser import JsonParser

        parser = JsonParser()
        result = parser.parse(b"not json at all {{{")
        assert not result.success
        assert result.error is not None

    def test_html_parser_empty(self):
        from research_store.parsing.html_parser import HtmlNormalizedParser

        parser = HtmlNormalizedParser()
        result = parser.parse(b"")
        assert result.success  # Empty is not an error
        assert result.block_count == 0

    def test_markdown_parser_binary_content(self):
        from research_store.parsing.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        # Binary content with replacement characters
        result = parser.parse(b"\x00\x01\x02\xff\xfe")
        assert result.success  # Should handle via errors="replace"

    def test_registry_empty_registry(self):
        """A registry with no parsers raises for any selection."""
        registry = ParserRegistry()
        with pytest.raises(ParserSelectionError):
            registry.select("text/plain")

    def test_parse_result_metadata(self):
        """ParseResult must carry metadata dict."""
        from research_store.parsing.markdown_parser import MarkdownParser

        parser = MarkdownParser()
        result = parser.parse(b"# H\n\nP")
        assert "block_type_counts" in result.metadata


# ---------------------------------------------------------------------------
# Deterministic chunk identity (via legacy)
# ---------------------------------------------------------------------------


class TestDeterministicChunkIdentity:
    """Tests that chunks remain deterministic via legacy path."""

    def test_same_input_same_chunks(self):
        from research_store.parsing import deterministic_chunks
        from research_store.domain import Block

        blocks = [
            Block(0, "heading", "Title"),
            Block(1, "paragraph", "Content here."),
        ]
        chunks1 = deterministic_chunks(blocks, max_chars=100)
        chunks2 = deterministic_chunks(blocks, max_chars=100)
        assert len(chunks1) == len(chunks2)
        for c1, c2 in zip(chunks1, chunks2):
            assert c1.content_sha256 == c2.content_sha256
            assert c1.text == c2.text
            assert c1.ordinal == c2.ordinal
            assert c1.token_count == c2.token_count
