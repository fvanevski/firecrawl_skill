"""Tests for DOM-aware HTML extraction (issue #43).

Covers:
- HTML corpus tests (headings, lists, tables, code, links, images, metadata)
- Malformed HTML tests
- Structure preservation tests
- Fallback policy tests (main-content → normalized → legacy)
- Registry selection tests
- Extractor and fallback version recording
- Raw-to-normalized provenance
- Boilerplate stripping via semantic elements
- Empty and minimal HTML
- Mixed structural elements

.. versionadded:: P5-03
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))


# ---------------------------------------------------------------------------
# HTML corpus tests — structural element preservation
# ---------------------------------------------------------------------------


class TestHtmlMainContentCorpus:
    """Tests that structural elements survive representative HTML fixtures."""

    def _parser(self):
        from research_store.parsing.html_main_content import HtmlMainContentParser

        return HtmlMainContentParser()

    def test_headings(self):
        parser = self._parser()
        source = (
            b"<h1>Main Title</h1>"
            b"<h2>Section A</h2>"
            b"<h3>Subsection</h3>"
            b"<h2>Section B</h2>"
        )
        result = parser.parse(source)
        assert result.success
        headings = [b for b in result.blocks if b.block_type == "heading"]
        assert len(headings) == 4
        assert headings[0].text == "Main Title"
        assert headings[0].heading_path == ()
        assert headings[1].text == "Section A"
        assert headings[1].heading_path == ("Main Title",)
        assert headings[2].text == "Subsection"
        assert headings[2].heading_path == ("Main Title", "Section A")
        assert headings[3].text == "Section B"
        assert headings[3].heading_path == ("Main Title",)

    def test_paragraphs(self):
        parser = self._parser()
        source = b"<p>First paragraph.</p><p>Second paragraph.</p>"
        result = parser.parse(source)
        assert result.success
        paragraphs = [b for b in result.blocks if b.block_type == "paragraph"]
        assert len(paragraphs) == 2
        assert "First paragraph" in paragraphs[0].text
        assert "Second paragraph" in paragraphs[1].text

    def test_lists(self):
        parser = self._parser()
        source = b"<ul><li>Item one</li><li>Item two</li><li>Item three</li></ul>"
        result = parser.parse(source)
        assert result.success
        items = [b for b in result.blocks if b.block_type == "list_item"]
        assert len(items) == 3
        assert "Item one" in items[0].text
        assert "Item two" in items[1].text
        assert "Item three" in items[2].text

    def test_nested_lists(self):
        """Nested lists should produce sequential list items."""
        parser = self._parser()
        source = (
            b"<ul><li>Parent 1<ul><li>Child 1</li><li>Child 2</li></ul></li>"
            b"<li>Parent 2</li></ul>"
        )
        result = parser.parse(source)
        assert result.success
        items = [b for b in result.blocks if b.block_type == "list_item"]
        assert len(items) >= 3

    def test_tables(self):
        parser = self._parser()
        source = (
            b"<table>"
            b"<tr><th>Name</th><th>Age</th></tr>"
            b"<tr><td>Alice</td><td>30</td></tr>"
            b"<tr><td>Bob</td><td>25</td></tr>"
            b"</table>"
        )
        result = parser.parse(source)
        assert result.success
        rows = [b for b in result.blocks if b.block_type == "table_row"]
        assert len(rows) == 3
        assert "| Name | Age |" in rows[0].text
        assert "| Alice | 30 |" in rows[1].text
        assert "| Bob | 25 |" in rows[2].text

    def test_code_blocks(self):
        parser = self._parser()
        source = b"<pre><code>def hello():\n    print('world')</code></pre>"
        result = parser.parse(source)
        assert result.success
        codes = [b for b in result.blocks if b.block_type == "code"]
        assert len(codes) == 1
        assert "def hello" in codes[0].text
        assert "print" in codes[0].text

    def test_links(self):
        parser = self._parser()
        source = b'<p>Visit <a href="https://example.com">Example</a> now.</p>'
        result = parser.parse(source)
        assert result.success
        paragraphs = [b for b in result.blocks if b.block_type == "paragraph"]
        assert len(paragraphs) == 1
        assert "[Example](https://example.com)" in paragraphs[0].text

    def test_images_and_captions(self):
        parser = self._parser()
        source = b'<img alt="A beautiful sunset" src="sunset.jpg">'
        result = parser.parse(source)
        assert result.success
        captions = [b for b in result.blocks if b.block_type == "caption"]
        assert len(captions) == 1
        assert "A beautiful sunset" in captions[0].text

    def test_horizontal_rules(self):
        parser = self._parser()
        source = b"<hr>"
        result = parser.parse(source)
        assert result.success
        hrs = [b for b in result.blocks if b.block_type == "horizontal_rule"]
        assert len(hrs) == 1
        assert hrs[0].text == "---"

    def test_blockquotes(self):
        parser = self._parser()
        source = b"<blockquote><p>A quoted passage.</p></blockquote>"
        result = parser.parse(source)
        assert result.success
        quotes = [b for b in result.blocks if b.block_type == "paragraph"]
        assert len(quotes) == 1
        assert "> A quoted passage" in quotes[0].text

    def test_metadata_extraction(self):
        parser = self._parser()
        source = (
            b'<html lang="en"><head>'
            b"<title>Page Title</title>"
            b'<meta name="description" content="A description">'
            b'<meta property="og:title" content="OG Title">'
            b'<meta property="og:description" content="OG Desc">'
            b'<meta property="og:image" content="https://example.com/img.png">'
            b'<link rel="canonical" href="https://example.com/page">'
            b"</head><body><p>Content</p></body></html>"
        )
        result = parser.parse(source)
        assert result.success
        meta = result.metadata.get("metadata", {})
        assert meta.get("title") == "Page Title"
        assert meta.get("description") == "A description"
        assert meta.get("og_title") == "OG Title"
        assert meta.get("og_description") == "OG Desc"
        assert meta.get("og_image") == "https://example.com/img.png"
        assert meta.get("canonical") == "https://example.com/page"
        assert meta.get("language") == "en"

    def test_main_content_extraction(self):
        """Content inside <main> should be extracted; nav/aside should be skipped."""
        parser = self._parser()
        source = (
            b"<nav><a href='/home'>Home</a></nav>"
            b"<aside>Sidebar content</aside>"
            b"<main><h1>Main Article</h1><p>Article body.</p></main>"
        )
        result = parser.parse(source)
        assert result.success
        # Should have heading and paragraph from main content
        headings = [b for b in result.blocks if b.block_type == "heading"]
        paragraphs = [b for b in result.blocks if b.block_type == "paragraph"]
        assert len(headings) == 1
        assert headings[0].text == "Main Article"
        assert len(paragraphs) >= 1
        # Nav and aside content should NOT appear
        all_text = " ".join(b.text for b in result.blocks)
        assert "Home" not in all_text
        assert "Sidebar" not in all_text

    def test_article_extraction(self):
        """When no <main>, <article> content should be extracted."""
        parser = self._parser()
        source = (
            b"<header>Site header</header>"
            b"<article><h1>Article Title</h1><p>Article body.</p></article>"
            b"<footer>Footer</footer>"
        )
        result = parser.parse(source)
        assert result.success
        headings = [b for b in result.blocks if b.block_type == "heading"]
        assert len(headings) == 1
        assert headings[0].text == "Article Title"
        # Header and footer content should be skipped
        all_text = " ".join(b.text for b in result.blocks)
        assert "Site header" not in all_text
        assert "Footer" not in all_text

    def test_mixed_structural_elements(self):
        """A realistic page with mixed elements should preserve structure."""
        parser = self._parser()
        source = (
            b"<html><head><title>Test</title></head><body>"
            b"<nav><a href='#'>Nav</a></nav>"
            b"<main>"
            b"<h1>Title</h1>"
            b"<p>Paragraph one.</p>"
            b"<ul><li>List 1</li><li>List 2</li></ul>"
            b"<blockquote><p>Quote text.</p></blockquote>"
            b"<pre><code>code snippet</code></pre>"
            b"<table><tr><th>H1</th><th>H2</th></tr><tr><td>A</td><td>B</td></tr></table>"
            b'<img alt="caption text">'
            b"<hr>"
            b"</main></body></html>"
        )
        result = parser.parse(source)
        assert result.success
        types = set(b.block_type for b in result.blocks)
        assert "heading" in types
        assert "paragraph" in types
        assert "list_item" in types
        assert "quotation" in types or "paragraph" in types
        assert "code" in types
        assert "table_row" in types
        assert "caption" in types
        assert "horizontal_rule" in types
        # Nav content should be stripped
        all_text = " ".join(b.text for b in result.blocks)
        assert "Nav" not in all_text

    def test_parser_version(self):
        parser = self._parser()
        assert parser.parser_version == "html-main-content-v1"

    def test_extractor_and_fallback_versions_in_metadata(self):
        parser = self._parser()
        result = parser.parse(b"<p>Hello</p>")
        assert result.success
        meta = result.metadata
        assert meta.get("extractor_version") == "html-main-content-v1"
        assert meta.get("fallback_version") == "html-normalized-v1"

    def test_empty_html(self):
        parser = self._parser()
        result = parser.parse(b"")
        assert result.success
        assert result.block_count == 0

    def test_whitespace_only(self):
        parser = self._parser()
        result = parser.parse(b"   \n\n  ")
        assert result.success
        assert result.block_count == 0

    def test_comments_stripped(self):
        parser = self._parser()
        source = b"<p>Visible</p><!-- comment --><p>Also visible</p>"
        result = parser.parse(source)
        assert result.success
        all_text = " ".join(b.text for b in result.blocks)
        assert "Visible" in all_text
        assert "comment" not in all_text.lower()

    def test_script_tags_ignored(self):
        parser = self._parser()
        source = b"<script>alert('hello')</script><p>Real content</p>"
        result = parser.parse(source)
        assert result.success
        all_text = " ".join(b.text for b in result.blocks)
        assert "alert" not in all_text
        assert "Real content" in all_text

    def test_style_tags_ignored(self):
        parser = self._parser()
        source = b"<style>.foo { color: red; }</style><p>Styled content</p>"
        result = parser.parse(source)
        assert result.success
        all_text = " ".join(b.text for b in result.blocks)
        assert "color" not in all_text
        assert "Styled content" in all_text

    def test_source_length(self):
        parser = self._parser()
        source = b"<h1>Hello</h1><p>World</p>"
        result = parser.parse(source)
        assert result.source_length == len(source.decode("utf-8"))


# ---------------------------------------------------------------------------
# Malformed HTML tests
# ---------------------------------------------------------------------------


class TestMalformedHtml:
    """Tests that malformed HTML is handled gracefully."""

    def _parser(self):
        from research_store.parsing.html_main_content import HtmlMainContentParser

        return HtmlMainContentParser()

    def test_unclosed_tags(self):
        """Unclosed tags should not crash and produce empty or partial blocks."""
        parser = self._parser()
        source = b"<div><p>Unclosed paragraph<div>Nested div"
        result = parser.parse(source)
        assert result.block_count == 0 or (result.success and result.block_count <= 3)

    def test_mismatched_tags(self):
        """Mismatched tags should not crash and produce empty or partial blocks."""
        parser = self._parser()
        source = b"<p>Para</div></p><p>Next</p>"
        result = parser.parse(source)
        assert result.block_count == 0 or (result.success and result.block_count <= 3)

    def test_self_closing_malformed(self):
        """Self-closing malformed HTML should not crash."""
        parser = self._parser()
        source = b"<br><hr><img src=x>"
        result = parser.parse(source)
        assert result.block_count == 0 or (result.success and result.block_count <= 3)

    def test_binary_data(self):
        """Binary data that cannot be usefully parsed returns empty blocks."""
        parser = self._parser()
        # Invalid UTF-8 sequence — decoded with replacement chars, no blocks
        source = b"\xff\xfe\x00\x01"
        result = parser.parse(source)
        assert result.success
        assert result.block_count <= 1

    def test_severely_malformed(self):
        """Severely malformed input should not crash and produce empty or partial blocks."""
        parser = self._parser()
        source = b"<<<>>>???<<<>>>{{{"
        result = parser.parse(source)
        assert result.block_count == 0 or (result.success and result.block_count <= 3)

    def test_nested_open_tags(self):
        parser = self._parser()
        source = b"<ul><li>One<li>Two<li>Three</ul>"
        result = parser.parse(source)
        assert result.success
        items = [b for b in result.blocks if b.block_type == "list_item"]
        assert len(items) == 3

    def test_deeply_nested(self):
        parser = self._parser()
        source = b"<div><div><div><div><p>Deep</p></div></div></div></div>"
        result = parser.parse(source)
        assert result.success
        paragraphs = [b for b in result.blocks if b.block_type == "paragraph"]
        assert len(paragraphs) >= 1


# ---------------------------------------------------------------------------
# Fallback policy tests
# ---------------------------------------------------------------------------


class TestFallbackPolicy:
    """Tests that the fallback chain prefers DOM parsing over regex."""

    def test_registry_selects_main_content_parser(self):
        from research_store.parsing import build_default_registry

        registry = build_default_registry()
        record = registry.select("text/html")
        assert "HtmlMainContentParser" in record.selected_parser_type
        assert record.selection_method == "exact"

    def test_main_content_parser_version(self):
        from research_store.parsing.html_main_content import HtmlMainContentParser

        assert HtmlMainContentParser.parser_version == "html-main-content-v1"

    def test_normalized_parser_available_as_fallback(self):
        from research_store.parsing import build_default_registry

        registry = build_default_registry()
        record = registry.select("text/html-fallback")
        assert "HtmlNormalizedParser" in record.selected_parser_type

    def test_html_fails_without_registry(self):
        """CorpusService._parse_content raises ValueError for HTML if no parser is available."""
        from research_store.service import CorpusService
        import pytest

        # Minimal mock config
        config = type(
            "Config",
            (),
            {
                "parser_version": "test-v1",
                "chunker_version": "test-v1",
                "normalization_version": "test-v1",
            },
        )()
        service = CorpusService(
            config=config,
            uow_factory=lambda: None,
            blob_store=None,
            parser_registry=None,  # No registry
        )
        raw = b"<h1>Hello</h1><p>World</p>"
        with pytest.raises(ValueError, match="HTML parsing failed"):
            service._parse_content(raw, "text/html")

    def test_html_fallback_chain(self):
        """When main-content parser fails, normalized parser is tried."""
        from research_store.service import CorpusService
        from research_store.parsing import build_default_registry

        config = type(
            "Config",
            (),
            {
                "parser_version": "test-v1",
                "chunker_version": "test-v1",
                "normalization_version": "test-v1",
            },
        )()
        registry = build_default_registry()
        service = CorpusService(
            config=config,
            uow_factory=lambda: None,
            blob_store=None,
            parser_registry=registry,
        )

        # Valid HTML — should use main-content parser
        raw = b"<main><h1>Title</h1><p>Body</p></main>"
        blocks = service._parse_content(raw, "text/html")
        assert len(blocks) >= 1
        types = [b.block_type for b in blocks]
        assert "heading" in types

    def test_normalized_fallback_when_primary_fails(self):
        """When the primary HtmlMainContentParser raises, the normalized
        HtmlNormalizedParser is tried as an intermediate fallback."""
        from unittest.mock import patch

        from research_store.service import CorpusService
        from research_store.parsing import build_default_registry

        config = type(
            "Config",
            (),
            {
                "parser_version": "test-v1",
                "chunker_version": "test-v1",
                "normalization_version": "test-v1",
            },
        )()
        registry = build_default_registry()
        service = CorpusService(
            config=config,
            uow_factory=lambda: None,
            blob_store=None,
            parser_registry=registry,
        )

        # HTML that the main-content parser will parse successfully,
        # but we patch it to raise so the fallback path is exercised.
        raw = b"<main><h1>Title</h1><p>Body</p></main>"

        with patch(
            "research_store.parsing.html_main_content.HtmlMainContentParser.parse",
            side_effect=ValueError("simulated failure"),
        ), patch(
            "research_store.service.CorpusService._try_normalized_html",
            return_value=[{"type": "paragraph", "text": "mocked"}],
        ):
            blocks = service._parse_content(raw, "text/html")
            # Should fall through to normalized fallback, which succeeds
            assert len(blocks) >= 1

    def test_html_parsing_fails_when_both_parsers_fail(self):
        """When both primary and normalized parsers fail, a ValueError is raised."""
        from unittest.mock import patch
        import pytest

        from research_store.service import CorpusService
        from research_store.parsing import build_default_registry

        config = type(
            "Config",
            (),
            {
                "parser_version": "test-v1",
                "chunker_version": "test-v1",
                "normalization_version": "test-v1",
            },
        )()
        registry = build_default_registry()
        service = CorpusService(
            config=config,
            uow_factory=lambda: None,
            blob_store=None,
            parser_registry=registry,
        )

        raw = b"<main><h1>Title</h1><p>Body</p></main>"

        with patch(
            "research_store.parsing.html_main_content.HtmlMainContentParser.parse",
            side_effect=ValueError("simulated failure"),
        ), patch(
            "research_store.service.CorpusService._try_normalized_html",
            return_value=None,
        ):
            with pytest.raises(ValueError, match="HTML parsing failed"):
                service._parse_content(raw, "text/html")

    def test_is_html_content(self):
        from research_store.service import CorpusService

        assert CorpusService._is_html_content("text/html", b"<html>")
        assert CorpusService._is_html_content("application/xhtml+xml", b"")
        assert CorpusService._is_html_content(None, b"<html><body>")
        assert not CorpusService._is_html_content("text/plain", b"plain text")
        assert not CorpusService._is_html_content(None, b"plain text")


# ---------------------------------------------------------------------------
# Raw-to-normalized provenance tests
# ---------------------------------------------------------------------------


class TestRawToNormalizedProvenance:
    """Tests that raw HTML and normalized output are properly linked."""

    def test_metadata_contains_version_info(self):
        from research_store.parsing.html_main_content import HtmlMainContentParser

        parser = HtmlMainContentParser()
        result = parser.parse(b"<p>Hello</p>")
        assert result.success
        meta = result.metadata
        # Extractor version is recorded
        assert "extractor_version" in meta
        # Fallback version is recorded
        assert "fallback_version" in meta
        # Block type counts are recorded
        assert "block_type_counts" in meta

    def test_parse_result_fields(self):
        from research_store.parsing.html_main_content import HtmlMainContentParser

        parser = HtmlMainContentParser()
        source = b"<h1>Title</h1><p>Body</p>"
        result = parser.parse(source, mime_type="text/html", source_length=20)
        assert result.success
        assert result.mime_type == "text/html"
        assert result.source_length == 20
        assert result.encoding == "utf-8"
        assert result.block_count == 2
        assert result.parser_version == "html-main-content-v1"

    def test_error_result_fields(self):
        from research_store.parsing.html_main_content import HtmlMainContentParser

        # Valid HTML that produces no blocks is still a success
        parser = HtmlMainContentParser()
        result = parser.parse(b"", mime_type="text/html")
        assert result.success is True
        assert result.error is None
        assert result.block_count == 0


# ---------------------------------------------------------------------------
# Legacy compatibility tests
# ---------------------------------------------------------------------------


class TestLegacyCompatibility:
    """Tests that typed blocks convert to legacy Block correctly."""

    def test_to_legacy_block(self):
        from research_store.parsing.html_main_content import HtmlMainContentParser
        from research_store.domain import Block

        parser = HtmlMainContentParser()
        result = parser.parse(b"<h1>Title</h1><p>Body</p>")
        legacy = result.to_legacy_blocks()
        assert len(legacy) == 2
        assert isinstance(legacy[0], Block)
        assert legacy[0].block_type == "heading"
        assert legacy[0].text == "Title"
        assert legacy[0].parser_version == "html-main-content-v1"

    def test_block_type_preserved(self):
        from research_store.parsing.html_main_content import HtmlMainContentParser

        parser = HtmlMainContentParser()
        source = (
            b"<h1>H</h1><p>P</p><ul><li>L</li></ul>"
            b"<table><tr><td>R</td></tr></table>"
            b"<blockquote><p>Q</p></blockquote>"
            b"<pre><code>C</code></pre>"
            b'<img alt="I">'
            b"<hr>"
        )
        result = parser.parse(source)
        assert result.success
        legacy = result.to_legacy_blocks()
        types = [b.block_type for b in legacy]
        assert "heading" in types
        assert "paragraph" in types
        assert "list_item" in types
        assert "table_row" in types
        assert "code" in types
        assert "caption" in types
        assert "horizontal_rule" in types


# ---------------------------------------------------------------------------
# Service layer integration tests
# ---------------------------------------------------------------------------


class TestServiceIntegration:
    """Integration tests for CorpusService with HTML content."""

    def test_parse_content_with_registry(self):
        from research_store.service import CorpusService
        from research_store.parsing import build_default_registry

        # Minimal mock config — _parse_content only uses parser_registry
        config = type(
            "Config",
            (),
            {
                "parser_version": "test-v1",
                "chunker_version": "test-v1",
                "normalization_version": "test-v1",
            },
        )()
        registry = build_default_registry()
        service = CorpusService(
            config=config,
            uow_factory=lambda: None,
            blob_store=None,
            parser_registry=registry,
        )

        # HTML content should be parsed by main-content parser
        raw = b"<main><h1>Title</h1><p>Body text.</p></main>"
        blocks = service._parse_content(raw, "text/html")
        assert len(blocks) >= 1
        types = [b.block_type for b in blocks]
        assert "heading" in types
        assert "paragraph" in types

    def test_parse_content_no_registry(self):
        from research_store.service import CorpusService
        import pytest

        config = type(
            "Config",
            (),
            {
                "parser_version": "test-v1",
                "chunker_version": "test-v1",
                "normalization_version": "test-v1",
            },
        )()
        service = CorpusService(
            config=config,
            uow_factory=lambda: None,
            blob_store=None,
            parser_registry=None,
        )

        raw = b"<p>Paragraph</p>"
        with pytest.raises(ValueError, match="HTML parsing failed"):
            service._parse_content(raw, "text/html")

    def test_parse_content_markdown(self):
        from research_store.service import CorpusService
        from research_store.parsing import build_default_registry

        config = type(
            "Config",
            (),
            {
                "parser_version": "test-v1",
                "chunker_version": "test-v1",
                "normalization_version": "test-v1",
            },
        )()
        registry = build_default_registry()
        service = CorpusService(
            config=config,
            uow_factory=lambda: None,
            blob_store=None,
            parser_registry=registry,
        )

        raw = b"# Heading\n\nParagraph text."
        blocks = service._parse_content(raw, "text/markdown")
        assert len(blocks) >= 1
        types = [b.block_type for b in blocks]
        assert "heading" in types

    def test_parse_content_plain_text(self):
        from research_store.service import CorpusService
        from research_store.parsing import build_default_registry

        config = type(
            "Config",
            (),
            {
                "parser_version": "test-v1",
                "chunker_version": "test-v1",
                "normalization_version": "test-v1",
            },
        )()
        registry = build_default_registry()
        service = CorpusService(
            config=config,
            uow_factory=lambda: None,
            blob_store=None,
            parser_registry=registry,
        )

        raw = b"Plain text content."
        blocks = service._parse_content(raw, "text/plain")
        assert len(blocks) >= 1
        assert blocks[0].block_type == "paragraph"

    def test_parse_content_json(self):
        from research_store.service import CorpusService
        from research_store.parsing import build_default_registry

        config = type(
            "Config",
            (),
            {
                "parser_version": "test-v1",
                "chunker_version": "test-v1",
                "normalization_version": "test-v1",
            },
        )()
        registry = build_default_registry()
        service = CorpusService(
            config=config,
            uow_factory=lambda: None,
            blob_store=None,
            parser_registry=registry,
        )

        raw = b'{"key": "value", "nested": {"inner": "data"}}'
        blocks = service._parse_content(raw, "application/json")
        assert len(blocks) >= 1

    def test_parse_content_unsupported_mime(self):
        from research_store.service import CorpusService
        from research_store.parsing import build_default_registry
        from research_store.parsing.interfaces import UnsupportedFormatError

        config = type(
            "Config",
            (),
            {
                "parser_version": "test-v1",
                "chunker_version": "test-v1",
                "normalization_version": "test-v1",
            },
        )()
        registry = build_default_registry()
        service = CorpusService(
            config=config,
            uow_factory=lambda: None,
            blob_store=None,
            parser_registry=registry,
        )

        # PDF is registered but raises UnsupportedFormatError
        with pytest.raises(UnsupportedFormatError):
            service._parse_content(b"%PDF-1.4 fake", "application/pdf")


# ---------------------------------------------------------------------------
# Content-sniffing tests
# ---------------------------------------------------------------------------


class TestContentSniffing:
    """Tests for _is_html_content helper."""

    def test_mime_type_html(self):
        from research_store.service import CorpusService

        assert CorpusService._is_html_content("text/html", b"")
        assert CorpusService._is_html_content("text/html; charset=utf-8", b"")
        assert CorpusService._is_html_content("application/xhtml+xml", b"")

    def test_mime_type_not_html(self):
        from research_store.service import CorpusService

        assert not CorpusService._is_html_content("text/markdown", b"")
        assert not CorpusService._is_html_content("text/plain", b"")
        assert not CorpusService._is_html_content("application/json", b"")

    def test_content_sniff_html(self):
        from research_store.service import CorpusService

        assert CorpusService._is_html_content(None, b"<html><body>")
        assert CorpusService._is_html_content(None, b"<main><article>")
        assert CorpusService._is_html_content(None, b"<!doctype html>")
        # <div> is no longer a sniff marker — it's too broad
        # These are the actual sniff markers
        assert CorpusService._is_html_content(None, b"<head><title>")
        assert CorpusService._is_html_content(None, b"<body><div>")

    def test_content_sniff_div_combined_with_marker(self):
        """<div> alone is not a sniff marker, but combined with a real marker
        the content is still detected as HTML."""
        from research_store.service import CorpusService

        assert not CorpusService._is_html_content(None, b"<div>just div</div>")
        assert CorpusService._is_html_content(None, b"<body><div>combined</div>")
        assert CorpusService._is_html_content(None, b"<main><div>combined</div>")
        assert CorpusService._is_html_content(None, b"<article><div>combined</div>")

    def test_content_sniff_not_html(self):
        from research_store.service import CorpusService

        assert not CorpusService._is_html_content(None, b"Plain text")
        assert not CorpusService._is_html_content(None, b"# Markdown")
        assert not CorpusService._is_html_content(None, b'{"json": true}')


# ---------------------------------------------------------------------------
# Representative fixture tests
# ---------------------------------------------------------------------------


class TestRepresentativeFixtures:
    """Tests using representative HTML fixtures that verify structural preservation."""

    def _parser(self):
        from research_store.parsing.html_main_content import HtmlMainContentParser

        return HtmlMainContentParser()

    def test_news_article_fixture(self):
        """A realistic news article HTML page."""
        parser = self._parser()
        source = (
            b'<!DOCTYPE html><html lang="en"><head>'
            b"<title>Breaking News: Major Discovery</title>"
            b'<meta name="description" content="Scientists discover...">'
            b"</head><body>"
            b'<nav class="navigation"><a href="/">Home</a><a href="/about">About</a></nav>'
            b'<header class="site-header">News Portal</header>'
            b"<main>"
            b"<article>"
            b"<h1>Breaking News: Major Discovery</h1>"
            b'<p class="byline">By <a href="/author">Jane Doe</a> -- January 1, 2026</p>'
            b"<p>Scientists at the university have made a groundbreaking discovery "
            b"that could change the way we understand the universe.</p>"
            b"<h2>Key Findings</h2>"
            b"<ul>"
            b"<li>First observation of the phenomenon</li>"
            b"<li>Published in <em>Nature</em> journal</li>"
            b"<li>Collaborative effort across three countries</li>"
            b"</ul>"
            b"<h2>Methodology</h2>"
            b"<p>The team used a novel approach combining "
            b"<a href='https://example.com/technique'>advanced techniques</a> "
            b"with traditional methods.</p>"
            b"<figure>"
            b'<img src="diagram.png" alt="Diagram of the experimental setup">'
            b"<figcaption>Figure 1: Experimental setup</figcaption>"
            b"</figure>"
            b"<blockquote>"
            b"<p>&quot;This is a landmark moment in science,&quot; said the lead researcher.</p>"
            b"</blockquote>"
            b"<h2>Code Example</h2>"
            b"<pre><code>import analysis\nresult = analyze(data)</code></pre>"
            b"</article>"
            b"</main>"
            b'<aside class="sidebar">Related: <a href="/related">More stories</a></aside>'
            b"<footer>&copy; 2026 News Portal</footer>"
            b"</body></html>"
        )
        result = parser.parse(source)
        assert result.success
        assert result.block_count > 0

        # Verify structural elements
        headings = [b for b in result.blocks if b.block_type == "heading"]
        assert len(headings) >= 3  # H1, H2, H2

        # Verify metadata
        meta = result.metadata.get("metadata", {})
        assert meta.get("title") == "Breaking News: Major Discovery"
        assert meta.get("description") == "Scientists discover..."
        assert meta.get("language") == "en"

        # Verify boilerplate is stripped
        all_text = " ".join(b.text for b in result.blocks)
        assert "News Portal" not in all_text  # header/footer text
        assert "Home" not in all_text  # nav text
        assert "Related" not in all_text  # sidebar text

        # Verify content is preserved
        assert "groundbreaking" in all_text.lower() or "discovery" in all_text.lower()

    def test_wiki_article_fixture(self):
        """A wiki-style article with tables and lists."""
        parser = self._parser()
        source = (
            b"<html><head><title>Wiki Article</title></head><body>"
            b"<nav>Navigation</nav>"
            b"<main>"
            b"<h1>Wiki Article Title</h1>"
            b"<p>This is the lead paragraph with <a href='/link'>a link</a>.</p>"
            b"<h2>History</h2>"
            b"<p>Early developments...</p>"
            b"<h2>Structure</h2>"
            b"<table>"
            b"<tr><th>Property</th><th>Value</th></tr>"
            b"<tr><td>Mass</td><td>10 kg</td></tr>"
            b"<tr><td>Volume</td><td>5 m3</td></tr>"
            b"</table>"
            b"<h2>References</h2>"
            b"<ol><li>Reference 1</li><li>Reference 2</li></ol>"
            b"</main>"
            b"<aside>Sidebar</aside>"
            b"</body></html>"
        )
        result = parser.parse(source)
        assert result.success
        assert result.block_count > 0

        # Verify wiki structure
        headings = [b for b in result.blocks if b.block_type == "heading"]
        assert len(headings) >= 4  # H1 + H2 x3

        tables = [b for b in result.blocks if b.block_type == "table_row"]
        assert len(tables) >= 3  # header row + 2 data rows

        # No sidebar content
        all_text = " ".join(b.text for b in result.blocks)
        assert "Sidebar" not in all_text
        assert "Navigation" not in all_text

    def test_document_with_images_and_captions(self):
        """A document with multiple images and captions."""
        parser = self._parser()
        source = (
            b"<main>"
            b"<h1>Photo Essay</h1>"
            b'<img alt="Sunset over the ocean">'
            b"<p>The sun sets beautifully.</p>"
            b'<img alt="Mountains at dawn">'
            b"<p>Mountains in the morning light.</p>"
            b'<img alt="City skyline at night">'
            b"</main>"
        )
        result = parser.parse(source)
        assert result.success
        captions = [b for b in result.blocks if b.block_type == "caption"]
        assert len(captions) == 3
        assert "Sunset over the ocean" in captions[0].text
        assert "Mountains at dawn" in captions[1].text
        assert "City skyline at night" in captions[2].text

    def test_html_offset_preservation(self):
        """Test that HTML offsets correctly map back to the original text."""
        parser = self._parser()
        source = b"<main>\n  <h1>Title</h1>\n  <p>Some paragraph text.</p>\n</main>"
        result = parser.parse(source)
        text = source.decode("utf-8")
        
        blocks = result.blocks
        assert len(blocks) == 2
        
        assert blocks[0].block_type == "heading"
        assert text[blocks[0].char_start:blocks[0].char_end] == "Title"
        
        assert blocks[1].block_type == "paragraph"
        assert text[blocks[1].char_start:blocks[1].char_end] == "Some paragraph text."

    def test_boilerplate_gating(self):
        """Test that boilerplate can be extracted if strip_boilerplate=False."""
        from research_store.parsing.html_main_content import HtmlMainContentParser
        parser = HtmlMainContentParser(strip_boilerplate=False)
        source = b"<nav>Navigation Link</nav><main>Main content</main><aside>Sidebar</aside>"
        result = parser.parse(source)
        
        all_text = " ".join(b.text for b in result.blocks)
        assert "Navigation Link" in all_text
        assert "Main content" in all_text
        assert "Sidebar" in all_text

    def test_figure_and_figcaption(self):
        """<figure>/<figcaption> elements are now explicitly handled."""
        parser = self._parser()
        source = (
            b"<main>"
            b"<h1>Document</h1>"
            b"<figure>"
            b'<img alt="A diagram">'
            b"<figcaption>Figure 1: Diagram description</figcaption>"
            b"</figure>"
            b"</main>"
        )
        result = parser.parse(source)
        assert result.success
        assert result.block_count > 0

        captions = [b for b in result.blocks if b.block_type == "caption"]
        assert len(captions) == 2
        assert "[A diagram]" in captions[0].text
        assert "Figure 1: Diagram description" in captions[1].text


# ---------------------------------------------------------------------------
# Integration test — full ingest round-trip
# ---------------------------------------------------------------------------


class TestIngestRoundTrip:
    """Integration test exercising the full pipeline:
    _prepare_ingest → _parse_content → deterministic_chunks.
    """

    def test_html_ingest_produces_blocks_and_chunks(self):
        """Verify that HTML content parsed by HtmlMainContentParser
        produces both blocks and chunks through the full pipeline."""
        from research_store.parsing import deterministic_chunks
        from research_store.parsing import build_default_registry

        # Minimal mock config
        config = type(
            "Config",
            (),
            {
                "parser_version": "test-v1",
                "chunker_version": "test-v1",
                "normalization_version": "test-v1",
            },
        )()
        registry = build_default_registry()

        # Use the actual service's _parse_content method
        from research_store.service import CorpusService

        service_obj = CorpusService(
            config=config,
            uow_factory=lambda: None,
            blob_store=None,
            parser_registry=registry,
        )

        # HTML content with multiple structural elements
        raw = (
            b"<main>"
            b"<h1>Test Article</h1>"
            b"<p>Introduction paragraph with <a href='https://example.com'>link</a>.</p>"
            b"<h2>Section</h2>"
            b"<ul><li>Item one</li><li>Item two</li></ul>"
            b"<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
            b"<pre><code>code snippet</code></pre>"
            b"</main>"
        )
        mime_type = "text/html"

        # Parse through service layer
        blocks = service_obj._parse_content(raw, mime_type)
        assert len(blocks) >= 1

        # Verify block types
        types = [b.block_type for b in blocks]
        assert "heading" in types
        assert "paragraph" in types
        assert "list_item" in types
        assert "table_row" in types
        assert "code" in types

        # Verify parser version is recorded
        for block in blocks:
            assert hasattr(block, "parser_version")
            assert block.parser_version == "html-main-content-v1"

        # Run deterministic_chunks on the blocks
        chunks = deterministic_chunks(blocks, max_chars=3000)
        assert len(chunks) >= 1

        # Verify chunk structure
        for chunk in chunks:
            assert hasattr(chunk, "text")
            assert hasattr(chunk, "content_sha256")
            assert hasattr(chunk, "token_count")
            assert chunk.token_count > 0
            assert chunk.content_sha256  # Non-empty hash

    def test_html_offsets_propagate_through_chunking(self):
        """HTML blocks have char_start/char_end populated; this must propagate
        through the chunking pipeline — chunks are produced with heading_path."""
        from research_store.parsing import deterministic_chunks
        from research_store.parsing import build_default_registry

        config = type(
            "Config",
            (),
            {
                "parser_version": "test-v1",
                "chunker_version": "test-v1",
                "normalization_version": "test-v1",
            },
        )()
        registry = build_default_registry()

        from research_store.service import CorpusService

        service_obj = CorpusService(
            config=config,
            uow_factory=lambda: None,
            blob_store=None,
            parser_registry=registry,
        )

        raw = b"<main><h1>Title</h1><p>Body text.</p></main>"
        blocks = service_obj._parse_content(raw, "text/html")

        # HTML blocks have offsets
        for block in blocks:
            assert block.char_start is not None
            assert block.char_end is not None
            assert block.char_start < block.char_end

        # Chunking must not fail with offsets
        chunks = deterministic_chunks(blocks, max_chars=3000)
        assert len(chunks) >= 1
        for chunk in chunks:
            assert hasattr(chunk, "heading_path")
