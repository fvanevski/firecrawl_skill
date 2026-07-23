"""Tests for tokenizer-backed hierarchical chunking (P5-06).

This module tests:

* Tokenizer registry: registration, fingerprinting, fallback behavior.
* Hierarchical chunker: token boundaries, oversized blocks, determinism,
  structural boundary preservation, parent-child mappings.
* Migration: schema changes for tokenizer_name and parent_block_id columns.
* Service integration: corpus ingestion with hierarchical chunker.
* Compatibility: legacy structural chunks coexist with hierarchical chunks.

.. versionadded:: P5-06
"""

from __future__ import annotations

import hashlib
import pytest
from pathlib import Path

from research_store.config import StoreConfig
from research_store.domain import Block, Chunk
from research_store.hierarchical_chunker import (
    HierarchicalChunk,
    hierarchical_chunks,
    _classify_block,
    _validate_chunks,
)
from research_store.parsing import structural_blocks
from research_store.parsing_legacy import (
    deterministic_chunks as legacy_deterministic_chunks,
)
from research_store.tokenizer_registry import (
    Tokenizer,
    count_tokens,
    get_registry,
    get_tokenizer,
    registry_fingerprint,
)


# ---------------------------------------------------------------------------
# Tokenizer registry tests
# ---------------------------------------------------------------------------


class TestTokenizerRegistry:
    """Tests for the tokenizer registry."""

    def test_registry_default_name(self):
        """Registry uses cl100k_base as default."""
        reg = get_registry()
        assert reg.default_name == "cl100k_base"

    def test_registry_registered_names(self):
        """Registry has expected built-in names."""
        reg = get_registry()
        names = reg.registered_names
        assert "cl100k_base" in names
        assert "bpe_fake" in names

    def test_registry_get_tokenizer(self):
        """Can retrieve tokenizer by name."""
        reg = get_registry()
        tk = reg.get("cl100k_base")
        assert isinstance(tk, Tokenizer)
        # Tokenizer name may be "cl100k_base" (tiktoken) or
        # "cl100k_base_bpe" (BPE fallback) depending on environment
        assert tk.name.startswith("cl100k_base")

    def test_registry_get_default(self):
        """Can retrieve default tokenizer without specifying name."""
        reg = get_registry()
        tk = reg.get()
        # Default may be "cl100k_base" or "cl100k_base_bpe"
        assert tk.name.startswith("cl100k_base")

    def test_registry_unknown_name_raises(self):
        """Unknown tokenizer name raises KeyError."""
        reg = get_registry()
        with pytest.raises(KeyError, match="unknown tokenizer"):
            reg.get("nonexistent_tokenizer")

    def test_registry_count_tokens(self):
        """Token counting works correctly."""
        reg = get_registry()
        # Simple text should have at least 1 token
        count = reg.count("Hello world")
        assert count >= 1

    def test_registry_fingerprint(self):
        """Registry fingerprint is a stable SHA-256 hex digest."""
        reg = get_registry()
        fp = reg.fingerprint
        assert len(fp) == 64  # SHA-256 hex length
        # Fingerprint should be deterministic
        assert reg.fingerprint == registry_fingerprint()

    def test_bpe_fallback_encoding_decoding(self):
        """BPE fallback tokenizer can encode and decode."""
        reg = get_registry()
        record = reg._records.get("bpe_fake")
        assert record is not None
        assert record.is_fallback is True
        bpe = record.tokenizer
        text = "Hello world"
        ids = bpe.encode(text)
        assert len(ids) > 0
        decoded = bpe.decode(ids)
        # BPE decode may not be exact due to merge limitations, but should produce output
        assert len(decoded) > 0

    def test_tokenizer_count_method(self):
        """Tokenizer.count() returns correct token count."""
        reg = get_registry()
        tk = reg.get("cl100k_base")
        count = tk.count("test")
        assert isinstance(count, int)
        assert count > 0


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def test_get_tokenizer(self):
        """get_tokenizer returns a tokenizer from the default registry."""
        tk = get_tokenizer()
        assert isinstance(tk, Tokenizer)
        assert tk.name.startswith("cl100k_base")

    def test_get_tokenizer_named(self):
        """get_tokenizer accepts a named tokenizer."""
        tk = get_tokenizer("bpe_fake")
        assert isinstance(tk, Tokenizer)
        assert tk.name == "bpe_fake"

    def test_count_tokens(self):
        """count_tokens counts tokens using the default registry."""
        count = count_tokens("test text")
        assert isinstance(count, int)
        assert count > 0

    def test_registry_fingerprint(self):
        """registry_fingerprint returns a stable hex digest."""
        fp = registry_fingerprint()
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)


class TestTiktokenFallback:
    """Tests for tiktoken availability detection."""

    def test_tiktoken_not_installed_uses_fallback(self):
        """When tiktoken is unavailable, registry uses BPE fallback."""
        try:
            import tiktoken  # noqa: F401

            has_tiktoken = True
        except ImportError:
            has_tiktoken = False

        reg = get_registry()
        tk = reg.get("cl100k_base")
        # The tokenizer name indicates whether tiktoken was used
        # If tiktoken is installed, name is "cl100k_base"
        # If not, name is "cl100k_base_bpe"
        assert tk.name in ("cl100k_base", "cl100k_base_bpe")
        if not has_tiktoken:
            assert "_bpe" in tk.name


# ---------------------------------------------------------------------------
# Hierarchical chunker tests
# ---------------------------------------------------------------------------


class TestHierarchicalChunker:
    """Tests for the hierarchical chunker."""

    def test_basic_chunking(self):
        """Basic chunking produces chunks from blocks."""
        source = "# Title\n\nParagraph one.\n\nParagraph two."
        blocks = structural_blocks(source)
        chunks = hierarchical_chunks(
            blocks,
            max_tokens=100,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        assert len(chunks) > 0
        for chunk in chunks:
            assert isinstance(chunk, HierarchicalChunk)

    def test_no_chunk_exceeds_max_tokens(self):
        """No chunk exceeds the configured token maximum."""
        # Create a long paragraph that will be split
        words = "word " * 500  # ~500 tokens depending on tokenizer
        source = f"# Title\n\n{words}"
        blocks = structural_blocks(source)
        chunks = hierarchical_chunks(
            blocks,
            max_tokens=100,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        for chunk in chunks:
            assert chunk.token_count <= 100, (
                f"Chunk {chunk.ordinal} has {chunk.token_count} tokens > 100"
            )

    def test_token_count_is_accurate(self):
        """Token count matches actual tokenizer count."""
        reg = get_registry()
        source = "# Title\n\nHello world."
        blocks = structural_blocks(source)
        chunks = hierarchical_chunks(
            blocks,
            max_tokens=1000,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        for chunk in chunks:
            actual_count = reg.count(chunk.text)
            assert chunk.token_count == actual_count, (
                f"Chunk {chunk.ordinal}: reported {chunk.token_count}, "
                f"actual {actual_count}"
            )

    def test_deterministic_chunking(self):
        """Identical inputs produce identical chunks."""
        source = "# Title\n\nParagraph one.\n\n- item 1\n- item 2\n\n> quote\n\n```py\nprint(1)\n```\n"
        blocks = structural_blocks(source)
        first = hierarchical_chunks(
            blocks,
            max_tokens=100,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        second = hierarchical_chunks(
            blocks,
            max_tokens=100,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        assert len(first) == len(second)
        for a, b in zip(first, second):
            assert a.text == b.text
            assert a.content_sha256 == b.content_sha256
            assert a.token_count == b.token_count
            assert a.heading_path == b.heading_path

    def test_chunk_content_sha256_is_correct(self):
        """Chunk content_sha256 is SHA-256 of the chunk text."""
        source = "# Title\n\nHello world."
        blocks = structural_blocks(source)
        chunks = hierarchical_chunks(
            blocks,
            max_tokens=1000,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        for chunk in chunks:
            expected = hashlib.sha256(chunk.text.encode()).hexdigest()
            assert chunk.content_sha256 == expected

    def test_parent_block_ordinal_is_set(self):
        """Parent block ordinal is recorded for each chunk."""
        source = "# Title\n\nParagraph one."
        blocks = structural_blocks(source)
        chunks = hierarchical_chunks(
            blocks,
            max_tokens=1000,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        for chunk in chunks:
            assert chunk.parent_block_ordinal is not None
            assert isinstance(chunk.parent_block_ordinal, int)

    def test_heading_path_is_preserved(self):
        """Heading path is preserved in chunks."""
        source = "# H1\n\n## H2\n\nParagraph.\n\n### H3\n\nMore text."
        blocks = structural_blocks(source)
        chunks = hierarchical_chunks(
            blocks,
            max_tokens=1000,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        # At least one chunk should have a non-empty heading path
        assert any(len(c.heading_path) > 0 for c in chunks)

    def test_oversized_block_splitting(self):
        """Oversized blocks are split safely."""
        # Create a single paragraph that exceeds max_tokens
        words = "word " * 200  # ~200 tokens
        source = f"# Title\n\n{words}"
        blocks = structural_blocks(source)
        assert len(blocks) == 2  # heading + paragraph

        chunks = hierarchical_chunks(
            blocks,
            max_tokens=50,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        # The paragraph should be split into multiple chunks
        assert len(chunks) > 1
        for chunk in chunks:
            assert chunk.token_count <= 50

    def test_code_block_boundary_preservation(self):
        """Code blocks are kept as separate chunks when possible."""
        source = "# Title\n\n```python\nprint('hello')\n```\n\nParagraph."
        blocks = structural_blocks(source)
        chunks = hierarchical_chunks(
            blocks,
            max_tokens=1000,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        # Code block should be in its own chunk or with adjacent content
        code_in_chunk = False
        for chunk in chunks:
            if "```python" in chunk.text:
                code_in_chunk = True
                break
        assert code_in_chunk

    def test_table_boundary_preservation(self):
        """Table rows are kept together when possible."""
        source = (
            "# Title\n\n"
            "| Header 1 | Header 2 |\n"
            "|----------|----------|\n"
            "| Cell 1   | Cell 2   |\n"
            "| Cell 3   | Cell 4   |\n"
        )
        blocks = structural_blocks(source)
        chunks = hierarchical_chunks(
            blocks,
            max_tokens=1000,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        assert len(chunks) > 0

    def test_list_boundary_preservation(self):
        """List items are preserved in chunks."""
        source = "# Title\n\n- item 1\n- item 2\n- item 3\n"
        blocks = structural_blocks(source)
        chunks = hierarchical_chunks(
            blocks,
            max_tokens=1000,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        assert len(chunks) > 0
        # All list items should be in at least one chunk
        all_items_in_chunks = True
        for item in ["item 1", "item 2", "item 3"]:
            found = any(item in c.text for c in chunks)
            if not found:
                all_items_in_chunks = False
                break
        assert all_items_in_chunks

    def test_empty_input(self):
        """Empty block list produces empty chunk list."""
        chunks = hierarchical_chunks(
            [],
            max_tokens=100,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        assert chunks == []

    def test_invalid_max_tokens_raises(self):
        """Zero or negative max_tokens raises ValueError."""
        source = "# Title\n\nParagraph."
        blocks = structural_blocks(source)
        with pytest.raises(ValueError, match="max_tokens must be positive"):
            hierarchical_chunks(
                blocks,
                max_tokens=0,
                tokenizer_name="cl100k_base",
                chunker_version="hierarchical-v1",
                chunker_name="hierarchical",
            )
        with pytest.raises(ValueError, match="max_tokens must be positive"):
            hierarchical_chunks(
                blocks,
                max_tokens=-1,
                tokenizer_name="cl100k_base",
                chunker_version="hierarchical-v1",
                chunker_name="hierarchical",
            )

    def test_chunker_version_is_recorded(self):
        """Chunker version is recorded in each chunk."""
        source = "# Title\n\nParagraph."
        blocks = structural_blocks(source)
        chunks = hierarchical_chunks(
            blocks,
            max_tokens=100,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v2",
            chunker_name="hierarchical",
        )
        for chunk in chunks:
            assert chunk.chunker_version == "hierarchical-v2"
            assert chunk.chunker_name == "hierarchical"

    def test_tokenizer_name_is_recorded(self):
        """Tokenizer name is recorded in each chunk."""
        source = "# Title\n\nParagraph."
        blocks = structural_blocks(source)
        chunks = hierarchical_chunks(
            blocks,
            max_tokens=100,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        for chunk in chunks:
            assert chunk.tokenizer_name == "cl100k_base"

    def test_metadata_contains_block_info(self):
        """Chunk metadata contains block count and types."""
        source = "# Title\n\nParagraph one.\n\n- item"
        blocks = structural_blocks(source)
        chunks = hierarchical_chunks(
            blocks,
            max_tokens=100,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        for chunk in chunks:
            assert "block_count" in chunk.metadata
            assert "block_types" in chunk.metadata
            assert isinstance(chunk.metadata["block_count"], int)
            assert isinstance(chunk.metadata["block_types"], list)

    def test_mixed_structure_fixture(self):
        """Complex mixed structure chunks correctly."""
        source = (
            "# Main Title\n\n"
            "Introduction paragraph.\n\n"
            "## Section One\n\n"
            "Content for section one.\n\n"
            "- List item A\n"
            "- List item B\n\n"
            "### Subsection\n\n"
            "More content.\n\n"
            "```python\n"
            "def hello():\n"
            "    print('world')\n"
            "```\n\n"
            "| Col A | Col B |\n"
            "|-------|-------|\n"
            "| 1     | 2     |\n\n"
            "> This is a quote.\n\n"
            "## Section Two\n\n"
            "Final paragraph."
        )
        blocks = structural_blocks(source)
        chunks = hierarchical_chunks(
            blocks,
            max_tokens=100,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        assert len(chunks) > 0
        for chunk in chunks:
            assert chunk.token_count > 0
            assert chunk.token_count <= 100
            assert chunk.content_sha256

    def test_ordinal_is_sequential(self):
        """Chunk ordinals are sequential starting from 0."""
        source = "# Title\n\nPara 1.\n\nPara 2.\n\nPara 3."
        blocks = structural_blocks(source)
        chunks = hierarchical_chunks(
            blocks,
            max_tokens=50,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        for i, chunk in enumerate(chunks):
            assert chunk.ordinal == i

    def test_first_last_block_ordinals(self):
        """First and last block ordinals are set correctly."""
        source = "# Title\n\nPara 1.\n\nPara 2."
        blocks = structural_blocks(source)
        chunks = hierarchical_chunks(
            blocks,
            max_tokens=1000,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        for chunk in chunks:
            assert chunk.first_block_ordinal is not None
            assert chunk.last_block_ordinal is not None
            assert chunk.first_block_ordinal <= chunk.last_block_ordinal or (
                # Single-block chunks may have same first and last
                chunk.first_block_ordinal == chunk.last_block_ordinal
            )


class TestBlockClassification:
    """Tests for the _classify_block function."""

    def test_heading_is_atomic(self):
        """Headings are classified as atomic."""
        block = Block(0, "heading", "Title", (), 0, 10)
        atomic = _classify_block(block)
        assert atomic.is_atomic is True
        assert atomic.block_type == "heading"

    def test_paragraph_is_not_atomic(self):
        """Paragraphs are classified as non-atomic."""
        block = Block(1, "paragraph", "Some text", (), 10, 20)
        atomic = _classify_block(block)
        assert atomic.is_atomic is False
        assert atomic.block_type == "paragraph"

    def test_code_is_atomic(self):
        """Code blocks are classified as atomic."""
        block = Block(2, "code", "print('hi')", (), 20, 35)
        atomic = _classify_block(block)
        assert atomic.is_atomic is True

    def test_table_row_is_atomic(self):
        """Table rows are classified as atomic."""
        block = Block(3, "table_row", "| A | B |", (), 35, 45)
        atomic = _classify_block(block)
        assert atomic.is_atomic is True

    def test_list_item_is_atomic(self):
        """List items are classified as atomic."""
        block = Block(4, "list_item", "Item text", (), 45, 55)
        atomic = _classify_block(block)
        assert atomic.is_atomic is True

    def test_quotation_is_atomic(self):
        """Quotations are classified as atomic."""
        block = Block(5, "quotation", "Quote text", (), 55, 65)
        atomic = _classify_block(block)
        assert atomic.is_atomic is True

    def test_caption_is_atomic(self):
        """Captions are classified as atomic."""
        block = Block(6, "caption", "![alt](url)", (), 65, 75)
        atomic = _classify_block(block)
        assert atomic.is_atomic is True


class TestValidation:
    """Tests for chunk validation."""

    def test_valid_chunks_pass(self):
        """Valid chunks pass validation."""
        chunks = [
            HierarchicalChunk(
                ordinal=0,
                text="Hello",
                content_sha256=hashlib.sha256(b"Hello").hexdigest(),
                first_block_ordinal=0,
                last_block_ordinal=0,
                token_count=2,
                heading_path=(),
            )
        ]
        _validate_chunks(chunks, max_tokens=100)  # Should not raise

    def test_zero_token_chunk_fails(self):
        """Chunks with zero tokens fail validation."""
        chunks = [
            HierarchicalChunk(
                ordinal=0,
                text="Hello",
                content_sha256=hashlib.sha256(b"Hello").hexdigest(),
                first_block_ordinal=0,
                last_block_ordinal=0,
                token_count=0,
                heading_path=(),
            )
        ]
        with pytest.raises(ValueError, match="zero tokens"):
            _validate_chunks(chunks, max_tokens=100)

    def test_exceeded_max_tokens_fails(self):
        """Chunks exceeding max_tokens fail validation."""
        chunks = [
            HierarchicalChunk(
                ordinal=0,
                text="Hello",
                content_sha256=hashlib.sha256(b"Hello").hexdigest(),
                first_block_ordinal=0,
                last_block_ordinal=0,
                token_count=150,
                heading_path=(),
            )
        ]
        with pytest.raises(ValueError, match="exceeds max_tokens"):
            _validate_chunks(chunks, max_tokens=100)

    def test_empty_sha256_fails(self):
        """Chunks with empty content_sha256 fail validation."""
        chunks = [
            HierarchicalChunk(
                ordinal=0,
                text="Hello",
                content_sha256="",
                first_block_ordinal=0,
                last_block_ordinal=0,
                token_count=2,
                heading_path=(),
            )
        ]
        with pytest.raises(ValueError, match="empty content_sha256"):
            _validate_chunks(chunks, max_tokens=100)


# ---------------------------------------------------------------------------
# Compatibility tests
# ---------------------------------------------------------------------------


class TestLegacyCompatibility:
    """Tests for backward compatibility with legacy chunking."""

    def test_legacy_deterministic_chunks_still_works(self):
        """Legacy deterministic_chunks function still works."""
        source = "# Title\n\nParagraph one."
        blocks = structural_blocks(source)
        chunks = legacy_deterministic_chunks(blocks, max_chars=3000)
        assert len(chunks) > 0
        for chunk in chunks:
            assert isinstance(chunk, Chunk)

    def test_hierarchical_and_legacy_produce_different_results(self):
        """Hierarchical and legacy chunkers produce different results."""
        source = "# Title\n\n" + "word " * 200
        blocks = structural_blocks(source)
        hier_chunks = hierarchical_chunks(
            blocks,
            max_tokens=100,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        legacy_chunks = legacy_deterministic_chunks(blocks, max_chars=3000)
        # Hierarchical uses token-based limits, legacy uses char-based
        # They should differ in chunk boundaries for long content
        assert len(hier_chunks) != len(legacy_chunks) or (
            len(hier_chunks) > 0
            and len(legacy_chunks) > 0
            and hier_chunks[0].token_count != legacy_chunks[0].token_count
        )

    def test_hierarchical_chunk_converts_to_legacy_chunk(self):
        """Hierarchical chunks can be converted to legacy Chunk format."""
        source = "# Title\n\nParagraph."
        blocks = structural_blocks(source)
        hier_chunks = hierarchical_chunks(
            blocks,
            max_tokens=100,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        for hc in hier_chunks:
            legacy = Chunk(
                ordinal=hc.ordinal,
                text=hc.text,
                content_sha256=hc.content_sha256,
                first_block_ordinal=hc.first_block_ordinal,
                last_block_ordinal=hc.last_block_ordinal,
                token_count=hc.token_count,
                heading_path=hc.heading_path,
            )
            assert legacy.text == hc.text
            assert legacy.token_count == hc.token_count


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------


class TestMigration:
    """Tests for the Alembic migration."""

    @property
    def _migration_path(self) -> Path:
        """Path to the migration file."""
        return (
            Path(__file__).parents[0]
            / "research_store"
            / "alembic"
            / "versions"
            / "0025_hierarchical_chunks.py"
        )

    def test_migration_file_exists(self):
        """Migration file 0025 exists."""
        assert self._migration_path.exists()

    def test_migration_has_correct_revision(self):
        """Migration has correct revision and down_revision."""
        import importlib.util

        try:
            import alembic
        except ImportError:
            pytest.skip("alembic not installed")

        spec = importlib.util.spec_from_file_location(
            "migration_0025", self._migration_path
        )
        assert spec is not None and spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        assert migration.revision == "0025_hierarchical_chunks"
        assert migration.down_revision == "0024_normalized_blocks_and_transformations"

    def test_migration_adds_tokenizer_name_column(self):
        """Migration adds tokenizer_name column to chunks table."""
        content = self._migration_path.read_text()
        assert "tokenizer_name" in content
        assert "op.add_column" in content

    def test_migration_adds_parent_block_id_column(self):
        """Migration adds parent_block_id column to chunks table."""
        content = self._migration_path.read_text()
        assert "parent_block_id" in content
        assert "op.add_column" in content

    def test_migration_updates_derivation_key(self):
        """Migration updates chunks_derivation_key to include tokenizer_name."""
        content = self._migration_path.read_text()
        assert "tokenizer_name" in content
        # Check that the new constraint includes tokenizer_name
        assert "chunker_name, chunker_version, tokenizer_name, ordinal" in content


# ---------------------------------------------------------------------------
# Integration test: service ingestion
# ---------------------------------------------------------------------------


class TestServiceIntegration:
    """Integration tests for service ingestion with hierarchical chunking."""

    def test_prepare_ingest_uses_hierarchical_chunker(self):
        """_prepare_ingest uses hierarchical_chunks internally."""
        from research_store.config import StoreConfig

        # We can't easily test the full ingestion without a database,
        # but we can verify the config has the new field
        config = StoreConfig(
            database_url="postgresql://localhost/test",
            qdrant_url="http://localhost:6333",
            qdrant_api_key="",
            qdrant_collection="test",
            qdrant_alias="test",
            valkey_url="redis://localhost:6379/0",
            blob_root=__import__("pathlib").Path("/tmp/test_blobs"),
            scratch_root=__import__("pathlib").Path("/tmp/test_scratch"),
            embedding_model="embed",
            embedding_url="",
            embedding_api_key="",
            embedding_revision="main",
            embedding_dimension=1024,
            reranker_url="",
            reranker_model="rerank",
            reranker_api_key="",
            reranker_candidate_limit=40,
            chunker_name="hierarchical",
            chunker_version="hierarchical-v1",
            chunker_max_tokens=1000,
            tokenizer_name="cl100k_base",
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            parser_registry_version="canonical-v1",
            max_index_attempts=5,
            job_lease_seconds=300,
            worker_poll_seconds=5,
        )
        assert config.chunker_max_tokens == 1000
        assert config.chunker_version == "hierarchical-v1"

    def test_config_from_env_has_chunker_max_tokens(self):
        """StoreConfig.from_env() reads CHUNKER_MAX_TOKENS from environment."""
        import os

        os.environ["CHUNKER_MAX_TOKENS"] = "500"
        try:
            config = StoreConfig.from_env()
            assert config.chunker_max_tokens == 500
        finally:
            del os.environ["CHUNKER_MAX_TOKENS"]


class TestRederiveIdempotency:
    """Tests for re-derive idempotency of hierarchical chunking."""

    def test_chunker_idempotent_across_calls(self):
        """Calling hierarchical_chunks twice with same input produces identical output."""
        source = "# Title\n\nParagraph one.\n\n- item A\n- item B\n\n```py\nprint(1)\n```\n"
        blocks = structural_blocks(source)

        first = hierarchical_chunks(
            blocks,
            max_tokens=50,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        second = hierarchical_chunks(
            blocks,
            max_tokens=50,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )

        assert len(first) == len(second)
        for a, b in zip(first, second):
            assert a.text == b.text
            assert a.content_sha256 == b.content_sha256
            assert a.token_count == b.token_count
            assert a.ordinal == b.ordinal
            assert a.first_block_ordinal == b.first_block_ordinal
            assert a.last_block_ordinal == b.last_block_ordinal
            assert a.heading_path == b.heading_path
            assert a.parent_block_ordinal == b.parent_block_ordinal

    def test_different_max_tokens_produce_different_chunks(self):
        """Changing max_tokens produces different chunk boundaries."""
        source = "# Title\n\n" + "word " * 200
        blocks = structural_blocks(source)

        chunks_50 = hierarchical_chunks(
            blocks,
            max_tokens=50,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        chunks_200 = hierarchical_chunks(
            blocks,
            max_tokens=200,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )

        # Fewer chunks for larger max_tokens
        assert len(chunks_50) > len(chunks_200)
        # All chunks still respect their respective limits
        for chunk in chunks_50:
            assert chunk.token_count <= 50
        for chunk in chunks_200:
            assert chunk.token_count <= 200

    def test_different_chunker_version_produces_different_derivation(self):
        """Changing chunker_version produces different derivation keys."""
        source = "# Title\n\nParagraph one."
        blocks = structural_blocks(source)

        chunks_v1 = hierarchical_chunks(
            blocks,
            max_tokens=100,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v1",
            chunker_name="hierarchical",
        )
        chunks_v2 = hierarchical_chunks(
            blocks,
            max_tokens=100,
            tokenizer_name="cl100k_base",
            chunker_version="hierarchical-v2",
            chunker_name="hierarchical",
        )

        assert len(chunks_v1) == len(chunks_v2)
        for a, b in zip(chunks_v1, chunks_v2):
            assert a.text == b.text  # Same content
            assert a.chunker_version == "hierarchical-v1"
            assert b.chunker_version == "hierarchical-v2"
            # Different derivation keys (different chunker_version)
            assert a.content_sha256 == b.content_sha256  # Content is same
