"""Canonical parser interfaces and typed adapters package.

This package provides a MIME-type-driven parser registry with typed block
output, offset preservation, and version tracking.

## Public API

* ``Parser`` — Protocol defining the parse interface.
* ``ParseResult`` — Typed parse result with blocks, metadata, and error field.
* ``TypedBlock`` — Immutable block with type tag, offsets, and heading path.
* ``ParserError`` — Base exception for parser failures.
* ``UnsupportedFormatError`` — Raised for explicitly unsupported MIME types.
* ``ParserSelectionError`` — Raised when no parser can be selected.
* ``ParserRegistry`` — MIME-type-driven registry with deterministic selection.
* ``SelectionRecord`` — Immutable record of a parser selection decision.
* ``get_registry()`` — Default (singleton) parser registry.
* ``build_default_registry()`` — Build a registry with built-in parsers.
* ``parse()`` — Convenience function: select + parse.
* ``structural_blocks()`` — Legacy Markdown structural parser (for compatibility).
* ``deterministic_chunks()`` — Legacy chunking function (for compatibility).
* ``hierarchical_chunks()`` — Tokenizer-backed hierarchical chunker.
* ``HierarchicalChunk`` — Chunk model with parent-child metadata.

## Built-in parsers

| Parser class               | MIME type(s)              | Version              |
|----------------------------|---------------------------|----------------------|
| ``MarkdownParser``         | ``text/markdown``         | ``markdown-v1``      |
| ``HtmlMainContentParser``  | ``text/html``             | ``html-main-content-v1`` |
| ``HtmlNormalizedParser``   | ``text/html-fallback``    | ``html-normalized-v1`` |
| ``JsonParser``             | ``application/json``      | ``json-v1``          |
| ``PlainTextParser``        | ``text/plain``            | ``text-v1``          |

## Extension points

| Parser class              | MIME type(s)          | Status |
|---------------------------|-----------------------|--------|
| ``PdfParser``             | ``application/pdf``   | Stub   |
| ``CodeParser``            | ``text/x-code``       | Stub   |
| ``LegalDocumentParser``   | ``application/x-legal`` | Stub   |

## Usage example

.. code-block:: python

    from research_store.parsing import parse, get_registry, UnsupportedFormatError

    # Convenience function
    result = parse(raw_bytes, mime_type="text/markdown")
    for block in result.blocks:
        print(f"  {block.block_type}: {block.text[:50]}...")

    # Direct registry usage
    registry = get_registry()
    record = registry.select("text/html", raw=html_bytes)
    parser = registry.get_parser("text/html")
    if parser:
        result = parser.parse(html_bytes, mime_type="text/html")

.. versionchanged:: P5-06
   Added ``hierarchical_chunks`` and ``HierarchicalChunk`` for
   tokenizer-backed hierarchical chunking.

.. versionchanged:: P5-04
   Introduced as part of Phase 5 canonical parser interfaces.
"""

from __future__ import annotations

# Hierarchical chunking (P5-06)
from ..hierarchical_chunker import (
    HierarchicalChunk,
    hierarchical_chunks,
)

# Legacy compatibility — re-export from the old parsing_legacy.py module
from ..parsing_legacy import (
    deterministic_chunks,
    extract_search_response_items,
    parse_raw_search_response,
    structural_blocks,
)

# Public API — interfaces
from .interfaces import (
    Parser,
    ParserError,
    ParseResult,
    ParserSelectionError,
    TypedBlock,
    UnsupportedFormatError,
)

# Public API — registry
from .registry import (
    ParserRegistry,
    SelectionRecord,
    build_default_registry,
    get_registry,
    parse,
)

__all__ = [
    "HierarchicalChunk",
    "ParseResult",
    "Parser",
    "ParserError",
    "ParserRegistry",
    "ParserSelectionError",
    "SelectionRecord",
    "TypedBlock",
    "UnsupportedFormatError",
    "build_default_registry",
    "deterministic_chunks",
    "extract_search_response_items",
    "get_registry",
    "hierarchical_chunks",
    "parse",
    "parse_raw_search_response",
    "structural_blocks",
]
