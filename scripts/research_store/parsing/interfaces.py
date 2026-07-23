"""Canonical parser interfaces and typed parse-result schema.

This module defines the abstract ``Parser`` protocol, the ``ParseResult``
dataclass that every adapter must return, and the ``ParserError`` exception
hierarchy used for unsupported-format and extraction-failure reporting.

## Design principles

* **Deterministic selection:** MIME type and document-type heuristics drive
  parser choice. The registry records which parser was selected.
* **Typed blocks:** Every parser returns ``TypedBlock`` instances carrying
  an explicit ``block_type`` tag, source offsets, and a heading path.
* **Version tracking:** Each parser reports its ``parser_version`` so that
  re-parsing with a newer adapter creates a new derivation.
* **Explicit failure:** Unsupported MIME types raise ``UnsupportedFormatError``
  rather than silently falling back to a generic parser.

## Domain model

* ``TypedBlock`` — immutable block with type, text, offsets, heading path.
* ``ParseResult`` — ordered list of blocks plus metadata (parser version,
  MIME type, source length, and an optional ``error`` field).
* ``Parser`` — protocol with a single ``parse()`` method.
* ``ParserError`` — base exception; ``UnsupportedFormatError`` is a subclass.

## Offset semantics

``char_start`` and ``char_end`` are **character offsets into the raw source
string** (after UTF-8 decoding). They are inclusive at the start and exclusive
at the end, matching Python string slicing semantics: ``source[start:end]``
yields the block text.  When a parser cannot determine offsets (e.g. a
JSON adapter that reconstructs text), both fields are ``None``.

## Parser version policy

Every parser implementation carries a ``parser_version`` class attribute.
The version is recorded in ``ParseResult.parser_version`` and propagated
into the ``parser_version`` column on the ``asset_snapshots`` row so that
reprocessing with a newer parser creates a **new derivation** rather than
mutating the existing snapshot.

.. versionchanged:: P5-04
   Introduced as part of Phase 5 canonical parser interfaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..domain import Block


# ---------------------------------------------------------------------------
# Block model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TypedBlock:
    """Immutable block produced by a typed parser adapter.

    Extends the legacy ``Block`` dataclass with an explicit ``block_type``
    tag and parser-version tracking.  Existing code that expects a
    ``Block`` can treat ``TypedBlock`` as a drop-in because all positional
    fields are preserved.

    Attributes:
        ordinal: Stable positional index within the parse result.
        block_type: Semantic type tag (e.g. ``"heading"``, ``"paragraph"``,
            ``"table_row"``).  Must be a lowercase ASCII identifier.
        text: Block content text.
        heading_path: Tuple of ancestor heading titles from root to current.
        char_start: Inclusive character offset into the source string.
        char_end: Exclusive character offset into the source string.
        parser_version: The parser version that produced this block.
        metadata: Free-form extension metadata.
    """

    ordinal: int
    block_type: str
    text: str
    heading_path: tuple[str, ...] = ()
    char_start: int | None = None
    char_end: int | None = None
    parser_version: str = "canonical-v1"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_legacy_block(self) -> Block:
        """Convert to a legacy ``Block`` for compatibility."""
        return Block(
            ordinal=self.ordinal,
            block_type=self.block_type,
            text=self.text,
            heading_path=self.heading_path,
            char_start=self.char_start,
            char_end=self.char_end,
            metadata=self.metadata,
        )


# ---------------------------------------------------------------------------
# Parse result schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParseResult:
    """Canonical parse result returned by every parser adapter.

    Attributes:
        blocks: Ordered list of typed blocks.
        parser_version: Deterministic parser version string.
        mime_type: The MIME type that was targeted.
        source_length: Length of the decoded source string in characters.
        encoding: Source encoding label (e.g. ``"utf-8"``).
        error: Non-empty error message when parsing failed partially or
            completely.  When present, ``blocks`` may be empty or partial.
        metadata: Free-form extension metadata (e.g. element counts).
    """

    blocks: list[TypedBlock]
    parser_version: str
    mime_type: str
    source_length: int
    encoding: str = "utf-8"
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """Return ``True`` when parsing produced a non-error result."""
        return self.error is None

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    @property
    def first_block_ordinal(self) -> int | None:
        if self.blocks:
            return self.blocks[0].ordinal
        return None

    @property
    def last_block_ordinal(self) -> int | None:
        if self.blocks:
            return self.blocks[-1].ordinal
        return None

    def to_legacy_blocks(self) -> list[Block]:
        """Convert to a list of legacy ``Block`` instances."""
        return [b.to_legacy_block() for b in self.blocks]


# ---------------------------------------------------------------------------
# Parser protocol
# ---------------------------------------------------------------------------


class Parser(Protocol):
    """Protocol that every parser adapter must implement.

    The protocol is intentionally minimal: every adapter receives raw bytes,
    a MIME type hint, and a version string, and must return a ``ParseResult``.

    Attributes:
        parser_version: Stable version string for this adapter
            (e.g. ``"markdown-v1"``, ``"html-normalized-v1"``).
    """

    parser_version: str

    def parse(
        self,
        raw: bytes,
        *,
        mime_type: str | None = None,
        source_length: int | None = None,
    ) -> ParseResult:
        """Parse *raw* bytes and return a typed ``ParseResult``.

        Args:
            raw: Raw byte payload (UTF-8 text, HTML, JSON, etc.).
            mime_type: MIME type hint from the scraper.  May be ``None``
                when the caller cannot determine the type.
            source_length: Pre-computed decoded string length.  When
                ``None``, the adapter must compute it internally.

        Returns:
            A ``ParseResult`` containing typed blocks and metadata.

        Raises:
            UnsupportedFormatError: When the MIME type or content is
                explicitly unsupported.
        """
        ...


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class ParserError(Exception):
    """Base exception for parser-level failures."""


class UnsupportedFormatError(ParserError):
    """Raised when a MIME type or document format is explicitly unsupported.

    Attributes:
        mime_type: The MIME type that was rejected.
        suggestion: Optional human-readable suggestion for the caller.
    """

    def __init__(
        self,
        mime_type: str | None,
        suggestion: str | None = None,
    ) -> None:
        self.mime_type = mime_type
        self.suggestion = suggestion
        message = f"unsupported format: {mime_type or 'unknown'}"
        if suggestion:
            message += f" — {suggestion}"
        super().__init__(message)


class ParserSelectionError(ParserError):
    """Raised when no parser can be selected for the given MIME type."""

    def __init__(self, mime_type: str | None, available: list[str]) -> None:
        self.mime_type = mime_type
        self.available = available
        super().__init__(
            f"no parser available for MIME type {mime_type!r}; "
            f"registered parsers: {available}"
        )
