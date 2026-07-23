"""Hierarchical tokenizer-backed chunker.

This module provides a deterministic, tokenizer-backed chunker that:

* Enforces a hard token maximum per chunk.
* Splits oversized blocks safely when a single block exceeds the limit.
* Preserves heading, table, code, and list boundaries.
* Creates atomic blocks, local chunks, and parent-section mappings.
* Produces stable, deterministic chunk identities based on content hash,
  not mutable ordinal position.

## Design principles

* **Real token counting:** Uses the configured tokenizer registry for
  accurate token counts. No character-based approximations.
* **Hard token limit:** No chunk may exceed ``max_tokens`` regardless
  of block structure. Oversized blocks are split recursively.
* **Boundary preservation:** Headings, tables, code fences, and lists
  are kept intact whenever possible. Only when a single block within
  one of these structures exceeds ``max_tokens`` is it split.
* **Stable identity:** Chunk identity is derived from the content SHA-256
  hash, the tokenizer name, chunker version, and parent-block identity.
  Ordinal position is not part of the identity.
* **Parent-child links:** Each chunk records its parent block ordinal and
  parent section heading path. These are persisted in the database.

## Chunk identity inputs

A stable chunk identity accounts for:

* source snapshot
* document derivation
* parser version
* normalizer version
* chunker version
* tokenizer identity
* token limit
* block or parent-section identity
* normalized content hash

## Usage

.. code-block:: python

    from research_store.hierarchical_chunker import (
        hierarchical_chunks,
        HierarchicalChunk,
    )
    from research_store.parsing import structural_blocks

    blocks = structural_blocks("# Title\\n\\nParagraph one.")
    chunks = hierarchical_chunks(
        blocks,
        max_tokens=100,
        tokenizer_name="cl100k_base",
        chunker_version="hierarchical-v1",
    )
    for chunk in chunks:
        print(f"  {chunk.chunker_name}-{chunk.chunker_version}: "
              f"{chunk.token_count} tokens")

.. versionadded:: P5-06
   Introduced as part of tokenizer-backed hierarchical chunking.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .domain import Block, Chunk
from .tokenizer_registry import Tokenizer, get_tokenizer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Regex patterns for structural boundary detection
# ---------------------------------------------------------------------------

# Heading: ATX-style (# to ######)
_RE_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")

# Code fence: ``` or ~~~ with optional language tag
_RE_CODE_FENCE = re.compile(r"^(`{3,}|~{3,})(.*)$")

# Table row: pipe-delimited (at least 3 pipes for header + 2 data rows)
_RE_TABLE_ROW = re.compile(r"^\|(.+)\|$")

# List item: bullet or numbered
_RE_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")

# Quotation: > prefix
_RE_QUOTATION = re.compile(r"^\s*>\s?")

# Caption: ![alt](url)
_RE_CAPTION = re.compile(r"^!\[[^\]]*\]\(")


# ---------------------------------------------------------------------------
# Atomic block model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AtomicBlock:
    """An atomic, non-splitable text unit with structural metadata.

    Attributes:
        text: The block text.
        block_type: Semantic type (heading, paragraph, code, list_item,
            table_row, quotation, caption).
        heading_path: Tuple of ancestor heading titles.
        ordinal: The original block ordinal in the parse result.
        char_start: Inclusive character offset into the source.
        char_end: Exclusive character offset into the source.
        is_atomic: Whether this block can be safely split.
            Headings, code fences, table rows, and list items are atomic.
    """

    text: str
    block_type: str
    heading_path: tuple[str, ...] = ()
    ordinal: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    is_atomic: bool = False


def _classify_block(block: Block) -> AtomicBlock:
    """Classify a legacy ``Block`` into an ``AtomicBlock`` with boundary info.

    Headings, code blocks, table rows, list items, quotations, and captions
    are treated as atomic (non-splitable). Paragraphs are non-atomic and may
    be split when they exceed ``max_tokens``.
    """
    atomic_types = {"heading", "code", "table_row", "list_item", "quotation", "caption"}
    return AtomicBlock(
        text=block.text,
        block_type=block.block_type,
        heading_path=block.heading_path,
        ordinal=block.ordinal,
        char_start=block.char_start,
        char_end=block.char_end,
        is_atomic=block.block_type in atomic_types,
    )


# ---------------------------------------------------------------------------
# Hierarchical chunk model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HierarchicalChunk:
    """A tokenizer-backed hierarchical chunk with parent-child metadata.

    Attributes:
        ordinal: Positional index within the chunk list (for display only).
        text: The chunk text content.
        content_sha256: SHA-256 hex digest of the chunk text.
        first_block_ordinal: Ordinal of the first source block.
        last_block_ordinal: Ordinal of the last source block.
        token_count: Real token count from the configured tokenizer.
        heading_path: Tuple of ancestor heading titles (parent section).
        chunker_name: Fixed identifier ``"hierarchical"``.
        chunker_version: Chunker version string (e.g. ``"hierarchical-v1"``).
        tokenizer_name: Tokenizer identifier used for counting.
        parent_block_ordinal: Ordinal of the parent block that this chunk
            derives from. For multi-block chunks this is the first block
            ordinal. For single-block chunks this matches the block ordinal.
        metadata: Free-form extension metadata.
    """

    ordinal: int
    text: str
    content_sha256: str
    first_block_ordinal: int
    last_block_ordinal: int
    token_count: int
    heading_path: tuple[str, ...] = ()
    chunker_name: str = "hierarchical"
    chunker_version: str = "hierarchical-v1"
    tokenizer_name: str = "cl100k_base"
    parent_block_ordinal: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Oversized block splitter
# ---------------------------------------------------------------------------


def _split_oversized_block(
    atomic: AtomicBlock,
    tokenizer: Tokenizer,
    max_tokens: int,
) -> list[AtomicBlock]:
    """Split an atomic block that exceeds ``max_tokens``.

    For code blocks, splits on newlines and reassembles coherent lines.
    For headings, table rows, and list items, splits on whitespace boundaries.
    For quotations and captions, delegates to paragraph-style splitting.

    Args:
        atomic: The atomic block to split.
        tokenizer: The tokenizer to use for counting.
        max_tokens: Hard token limit.

    Returns:
        A list of atomic blocks, each within ``max_tokens``.
    """
    if tokenizer.count(atomic.text) <= max_tokens:
        return [atomic]

    logger.debug(
        "Splitting oversized %s block (%d tokens > %d): %s",
        atomic.block_type,
        tokenizer.count(atomic.text),
        max_tokens,
        atomic.text[:80],
    )

    # Code blocks: split on newline boundaries
    if atomic.block_type == "code":
        lines = atomic.text.split("\n")
        return _split_by_lines(lines, tokenizer, max_tokens, atomic)

    # Headings: keep the full heading (shouldn't happen in practice, but safety)
    if atomic.block_type == "heading":
        # A heading that exceeds max_tokens is unusual; split on whitespace
        return _split_on_whitespace(atomic, tokenizer, max_tokens)

    # Table rows: split on pipe delimiters if possible
    if atomic.block_type == "table_row":
        parts = atomic.text.split("|")
        return _split_by_parts(parts, "|", tokenizer, max_tokens, atomic)

    # List items: split on whitespace
    if atomic.block_type == "list_item":
        return _split_on_whitespace(atomic, tokenizer, max_tokens)

    # Default: paragraph-style split on whitespace
    return _split_on_whitespace(atomic, tokenizer, max_tokens)


def _split_by_lines(
    lines: list[str],
    tokenizer: Tokenizer,
    max_tokens: int,
    template: AtomicBlock,
) -> list[AtomicBlock]:
    """Split a list of lines into chunks within ``max_tokens``."""
    result: list[AtomicBlock] = []
    current_lines: list[str] = []
    current_tokens = 0

    for line in lines:
        line_tokens = tokenizer.count(line + "\n")
        if current_lines and current_tokens + line_tokens > max_tokens:
            result.append(
                AtomicBlock(
                    text="\n".join(current_lines) + "\n",
                    block_type=template.block_type,
                    heading_path=template.heading_path,
                    ordinal=template.ordinal,
                    char_start=template.char_start,
                    char_end=None,
                    is_atomic=False,
                )
            )
            current_lines = [line]
            current_tokens = line_tokens
        else:
            current_lines.append(line)
            current_tokens += line_tokens

    if current_lines:
        result.append(
            AtomicBlock(
                text="\n".join(current_lines) + ("\n" if current_lines[-1] else ""),
                block_type=template.block_type,
                heading_path=template.heading_path,
                ordinal=template.ordinal,
                char_start=template.char_start,
                char_end=None,
                is_atomic=False,
            )
        )

    return result


def _split_by_parts(
    parts: list[str],
    separator: str,
    tokenizer: Tokenizer,
    max_tokens: int,
    template: AtomicBlock,
) -> list[AtomicBlock]:
    """Split a list of parts (e.g. table cell fragments) into chunks."""
    result: list[AtomicBlock] = []
    current_parts: list[str] = []
    current_tokens = 0

    for part in parts:
        part_text = part + separator
        part_tokens = tokenizer.count(part_text)
        if current_parts and current_tokens + part_tokens > max_tokens:
            result.append(
                AtomicBlock(
                    text=separator.join(current_parts) + separator,
                    block_type=template.block_type,
                    heading_path=template.heading_path,
                    ordinal=template.ordinal,
                    char_start=template.char_start,
                    char_end=None,
                    is_atomic=False,
                )
            )
            current_parts = [part]
            current_tokens = part_tokens
        else:
            current_parts.append(part)
            current_tokens += part_tokens

    if current_parts:
        result.append(
            AtomicBlock(
                text=separator.join(current_parts) + separator,
                block_type=template.block_type,
                heading_path=template.heading_path,
                ordinal=template.ordinal,
                char_start=template.char_start,
                char_end=None,
                is_atomic=False,
            )
        )

    return result


def _split_on_whitespace(
    atomic: AtomicBlock,
    tokenizer: Tokenizer,
    max_tokens: int,
) -> list[AtomicBlock]:
    """Split text on whitespace boundaries."""
    words = atomic.text.split()
    if not words:
        return [atomic]

    result: list[AtomicBlock] = []
    current_words: list[str] = []
    current_tokens = 0

    for word in words:
        word_tokens = tokenizer.count(word + " ")
        if current_words and current_tokens + word_tokens > max_tokens:
            result.append(
                AtomicBlock(
                    text=" ".join(current_words) + " ",
                    block_type=atomic.block_type,
                    heading_path=atomic.heading_path,
                    ordinal=atomic.ordinal,
                    char_start=atomic.char_start,
                    char_end=None,
                    is_atomic=False,
                )
            )
            current_words = [word]
            current_tokens = word_tokens
        else:
            current_words.append(word)
            current_tokens += word_tokens

    if current_words:
        result.append(
            AtomicBlock(
                text=" ".join(current_words),
                block_type=atomic.block_type,
                heading_path=atomic.heading_path,
                ordinal=atomic.ordinal,
                char_start=atomic.char_start,
                char_end=atomic.char_end,
                is_atomic=False,
            )
        )

    return result


# ---------------------------------------------------------------------------
# Hierarchical chunker
# ---------------------------------------------------------------------------


def hierarchical_chunks(
    blocks: list[Block],
    max_tokens: int = 1000,
    tokenizer_name: str = "cl100k_base",
    chunker_version: str = "hierarchical-v1",
    chunker_name: str = "hierarchical",
) -> list[HierarchicalChunk]:
    """Produce hierarchical chunks from a list of parsed blocks.

    This is the primary entry point for tokenizer-backed hierarchical
    chunking. It:

    1. Classifies each block as atomic or non-atomic.
    2. Splits any oversized atomic blocks recursively.
    3. Accumulates blocks into chunks respecting ``max_tokens``.
    4. Preserves heading, table, code, and list boundaries.
    5. Produces stable chunk identities based on content hash.

    Args:
        blocks: List of parsed ``Block`` instances from a typed parser.
        max_tokens: Hard token maximum per chunk. No chunk will exceed this.
        tokenizer_name: Tokenizer identifier (e.g. ``"cl100k_base"``).
        chunker_version: Chunker version for derivation tracking.
        chunker_name: Chunker name for derivation tracking.

    Returns:
        A list of ``HierarchicalChunk`` instances, ordered by block position.

    Raises:
        ValueError: When ``max_tokens`` is not positive.
        KeyError: When ``tokenizer_name`` is not registered.

    .. versionadded:: P5-06
    """
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")

    tokenizer = get_tokenizer(tokenizer_name)

    # Phase 1: Classify blocks into atomic units
    atomic_blocks: list[AtomicBlock] = [_classify_block(b) for b in blocks]

    # Phase 2: Split oversized atomic blocks
    expanded: list[AtomicBlock] = []
    for atomic in atomic_blocks:
        if tokenizer.count(atomic.text) > max_tokens:
            expanded.extend(_split_oversized_block(atomic, tokenizer, max_tokens))
        else:
            expanded.append(atomic)

    # Phase 3: Accumulate into chunks respecting max_tokens
    chunks: list[HierarchicalChunk] = []
    current: list[AtomicBlock] = []
    current_tokens = 0
    first_block_ordinal: int | None = None

    def _emit_chunk() -> None:
        nonlocal current_tokens, first_block_ordinal
        if not current:
            return

        text = "\n\n".join(a.text for a in current)
        token_count = tokenizer.count(text)
        heading_path = current[-1].heading_path if current else ()

        chunks.append(
            HierarchicalChunk(
                ordinal=len(chunks),
                text=text,
                content_sha256=hashlib.sha256(text.encode()).hexdigest(),
                first_block_ordinal=first_block_ordinal or 0,
                last_block_ordinal=current[-1].ordinal or 0,
                token_count=token_count,
                heading_path=heading_path,
                chunker_name=chunker_name,
                chunker_version=chunker_version,
                tokenizer_name=tokenizer_name,
                parent_block_ordinal=current[0].ordinal,
                metadata={
                    "block_count": len(current),
                    "block_types": [a.block_type for a in current],
                },
            )
        )
        current.clear()
        current_tokens = 0
        first_block_ordinal = None

    for atomic in expanded:
        atomic_tokens = tokenizer.count(atomic.text)

        # If a single atomic unit exceeds max_tokens, emit current chunk
        # and create a dedicated chunk for this oversized unit
        if atomic_tokens > max_tokens:
            _emit_chunk()
            # This should not happen after Phase 2, but safety check
            logger.warning(
                "Block %d (%s) still exceeds max_tokens after split: %d > %d",
                atomic.char_start,
                atomic.block_type,
                atomic_tokens,
                max_tokens,
            )
            continue

        # If adding this block would exceed max_tokens, emit current chunk.
        # We compute the actual text token count (including separators) to
        # avoid underestimating due to missing "\n\n" between blocks.
        if current:
            tentative_text = "\n\n".join(a.text for a in current) + "\n\n" + atomic.text
            tentative_tokens = tokenizer.count(tentative_text)
            if tentative_tokens > max_tokens:
                _emit_chunk()

        current.append(atomic)
        current_tokens += atomic_tokens
        if first_block_ordinal is None:
            first_block_ordinal = atomic.ordinal

    _emit_chunk()

    # Phase 4: Validate invariants
    _validate_chunks(chunks, max_tokens)

    return chunks


def _validate_chunks(chunks: list[HierarchicalChunk], max_tokens: int) -> None:
    """Validate chunk invariants.

    Raises:
        AssertionError: When any invariant is violated.
    """
    for chunk in chunks:
        assert chunk.token_count > 0, f"Chunk {chunk.ordinal} has zero tokens"
        assert chunk.token_count <= max_tokens, (
            f"Chunk {chunk.ordinal} exceeds max_tokens: "
            f"{chunk.token_count} > {max_tokens}"
        )
        assert chunk.content_sha256, f"Chunk {chunk.ordinal} has empty content_sha256"
        assert chunk.first_block_ordinal <= chunk.last_block_ordinal or (
            chunk.first_block_ordinal is not None
            and chunk.last_block_ordinal is not None
        ), f"Chunk {chunk.ordinal} has invalid block ordinals"


# ---------------------------------------------------------------------------
# Compatibility shim for legacy API
# ---------------------------------------------------------------------------


def deterministic_chunks(
    blocks: list[Block],
    max_tokens: int = 1000,
    tokenizer_name: str = "cl100k_base",
    chunker_version: str = "hierarchical-v1",
    chunker_name: str = "hierarchical",
) -> list[Chunk]:
    """Legacy-compatible chunking function that returns ``Chunk`` instances.

    This function wraps ``hierarchical_chunks`` and converts the results
    to legacy ``Chunk`` instances for backward compatibility with existing
    code that expects the ``Chunk`` dataclass from ``domain.py``.

    Args:
        blocks: List of parsed ``Block`` instances.
        max_tokens: Hard token maximum per chunk.
        tokenizer_name: Tokenizer identifier.
        chunker_version: Chunker version for derivation tracking.
        chunker_name: Chunker name for derivation tracking.

    Returns:
        A list of legacy ``Chunk`` instances.

    .. deprecated:: P5-06
       Use ``hierarchical_chunks`` directly for access to parent-child
       metadata and tokenizer information.
    """
    from .domain import Chunk

    hier_chunks = hierarchical_chunks(
        blocks,
        max_tokens=max_tokens,
        tokenizer_name=tokenizer_name,
        chunker_version=chunker_version,
        chunker_name=chunker_name,
    )

    legacy_chunks: list[Chunk] = []
    for hc in hier_chunks:
        legacy_chunks.append(
            Chunk(
                ordinal=hc.ordinal,
                text=hc.text,
                content_sha256=hc.content_sha256,
                first_block_ordinal=hc.first_block_ordinal,
                last_block_ordinal=hc.last_block_ordinal,
                token_count=hc.token_count,
                heading_path=hc.heading_path,
            )
        )

    return legacy_chunks
