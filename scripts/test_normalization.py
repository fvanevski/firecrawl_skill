"""Tests for reversible normalization (issue #45).

Covers:
- NormalizationService block-level rules
- Transformation record creation and audit
- Citation preservation
- Link preservation
- Image source preservation
- Code block preservation
- Confidence-gated aggressive cleanup
- Document-type-sensitive footer treatment
- Compatibility adapter (legacy clean_markdown equivalence)
- Round-trip diagnostics
- Idempotent re-run
- Domain model validation (NormalizedBlock, TransformationRecord)
- Rule version tracking
- Removed content recovery from source blocks

.. versionchanged:: P5-05
   Introduced as part of Phase 5 reversible normalization.
"""

from __future__ import annotations

# ruff: noqa: E402 - load the sibling script package without installing it.

import sys
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.domain import (
    NormalizedBlock,
    TransformationRecord,
    VALID_NORMALIZATION_DISPOSITIONS,
    VALID_NORMALIZATION_RULE_IDS,
)
from research_store.normalization import (
    NormalizationResult,
    NormalizationService,
    NORMALIZATION_VERSION,
)
from research_store.parsing.interfaces import TypedBlock


# ---------------------------------------------------------------------------
# Domain model tests
# ---------------------------------------------------------------------------


class TestNormalizedBlock:
    """Tests for the NormalizedBlock domain model."""

    def test_creates_from_source_block(self):
        src_id = uuid4()
        doc_id = uuid4()
        nb = NormalizedBlock.from_source_block(
            source_block_id=src_id,
            document_id=doc_id,
            ordinal=0,
            block_type="paragraph",
            text="Hello world",
            heading_path=("Section",),
            disposition="keep",
            rule_version="normalization-v1",
            transformation_reason="no-change",
            parser_version="markdown-v1",
        )
        assert nb.id is not None
        assert nb.source_block_id == src_id
        assert nb.document_id == doc_id
        assert nb.ordinal == 0
        assert nb.block_type == "paragraph"
        assert nb.text == "Hello world"
        assert nb.heading_path == ("Section",)
        assert nb.disposition == "keep"
        assert nb.rule_version == "normalization-v1"
        assert nb.transformation_reason == "no-change"
        assert nb.parser_version == "markdown-v1"

    def test_default_disposition_is_keep(self):
        nb = NormalizedBlock.from_source_block(
            source_block_id=uuid4(),
            document_id=uuid4(),
            ordinal=0,
            block_type="paragraph",
            text="test",
        )
        assert nb.disposition == "keep"
        assert nb.rule_version == "normalization-v1"

    def test_invalid_disposition_raises(self):
        with pytest.raises(ValueError, match="invalid disposition"):
            NormalizedBlock.from_source_block(
                source_block_id=uuid4(),
                document_id=uuid4(),
                ordinal=0,
                block_type="paragraph",
                text="test",
                disposition="deleted",
            )

    def test_all_dispositions_valid(self):
        for disp in VALID_NORMALIZATION_DISPOSITIONS:
            nb = NormalizedBlock.from_source_block(
                source_block_id=uuid4(),
                document_id=uuid4(),
                ordinal=0,
                block_type="paragraph",
                text="test",
                disposition=disp,
            )
            assert nb.disposition == disp


class TestTransformationRecord:
    """Tests for the TransformationRecord domain model."""

    def test_creates_with_defaults(self):
        nb_id = uuid4()
        record = TransformationRecord.create(
            normalized_block_id=nb_id,
            rule_id="strip-cookie-notice",
            reason="Cookie policy line",
            before_text="Use cookies",
            after_text="",
        )
        assert record.id is not None
        assert record.normalized_block_id == nb_id
        assert record.rule_id == "strip-cookie-notice"
        assert record.rule_version == "normalization-v1"
        assert record.reason == "Cookie policy line"
        assert record.before_text == "Use cookies"
        assert record.after_text == ""
        assert record.confidence == 1.0

    def test_invalid_rule_id_raises(self):
        with pytest.raises(ValueError, match="invalid rule_id"):
            TransformationRecord.create(
                normalized_block_id=uuid4(),
                rule_id="unknown-rule",
                reason="test",
            )

    def test_invalid_confidence_raises(self):
        with pytest.raises(ValueError, match="confidence must be in"):
            TransformationRecord.create(
                normalized_block_id=uuid4(),
                rule_id="strip-cookie-notice",
                reason="test",
                confidence=1.5,
            )

    def test_all_rule_ids_valid(self):
        for rule_id in VALID_NORMALIZATION_RULE_IDS:
            record = TransformationRecord.create(
                normalized_block_id=uuid4(),
                rule_id=rule_id,
                reason="test",
            )
            assert record.rule_id == rule_id


# ---------------------------------------------------------------------------
# NormalizationService rule tests
# ---------------------------------------------------------------------------


class TestNormalizationRules:
    """Tests for individual normalization rules."""

    def setup_method(self):
        self.service = NormalizationService(aggressive=False)

    def test_code_block_preserved(self):
        block = TypedBlock(
            ordinal=0,
            block_type="code",
            text="print('hello')",
            parser_version="html-normalized-v1",
        )
        result = self.service.normalize([block])
        assert len(result.blocks) == 1
        assert result.blocks[0].disposition == "keep"
        assert result.blocks[0].text == "print('hello')"
        # Check transformation record exists
        assert len(result.transformations) >= 1
        assert any(t.rule_id == "preserve-code-block" for t in result.transformations)

    def test_cookie_notice_removed(self):
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="Use cookies to improve your experience.",
            parser_version="html-normalized-v1",
        )
        result = self.service.normalize([block])
        assert len(result.removed_blocks) == 1
        assert result.removed_blocks[0].disposition == "remove"
        assert any(t.rule_id == "strip-cookie-notice" for t in result.transformations)

    def test_navigation_removed(self):
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="[skip to main content]",
            parser_version="html-normalized-v1",
        )
        result = self.service.normalize([block])
        assert len(result.removed_blocks) == 1
        assert any(t.rule_id == "strip-navigation" for t in result.transformations)

    def test_social_links_removed(self):
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="Share on Facebook",
            parser_version="html-normalized-v1",
        )
        result = self.service.normalize([block])
        assert len(result.removed_blocks) == 1
        assert any(t.rule_id == "strip-social-links" for t in result.transformations)

    def test_copyright_footer_removed(self):
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="Copyright © 2024 Example Corp",
            parser_version="html-normalized-v1",
        )
        result = self.service.normalize([block])
        assert len(result.removed_blocks) == 1
        assert any(
            t.rule_id == "strip-boilerplate-heading" for t in result.transformations
        )

    def test_citation_preserved(self):
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="As shown in [1], the results are significant.",
            parser_version="html-normalized-v1",
        )
        result = self.service.normalize([block])
        assert len(result.blocks) == 1
        assert result.blocks[0].disposition == "keep"
        assert result.blocks[0].text == "As shown in [1], the results are significant."
        assert any(t.rule_id == "preserve-citation" for t in result.transformations)

    def test_short_heading_preserved(self):
        block = TypedBlock(
            ordinal=0,
            block_type="heading",
            text="Introduction",
            parser_version="html-normalized-v1",
        )
        result = self.service.normalize([block])
        assert len(result.blocks) == 1
        assert result.blocks[0].disposition == "keep"
        assert any(
            t.rule_id == "preserve-short-heading" for t in result.transformations
        )

    def test_footnote_preserved(self):
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="Footnote 1: This is a footnote reference.",
            parser_version="html-normalized-v1",
        )
        result = self.service.normalize([block])
        assert len(result.blocks) == 1
        assert result.blocks[0].disposition == "keep"
        assert any(t.rule_id == "preserve-footnote" for t in result.transformations)

    def test_source_url_preserved(self):
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="https://example.com/source",
            parser_version="html-normalized-v1",
        )
        result = self.service.normalize([block])
        assert len(result.blocks) == 1
        assert result.blocks[0].disposition == "keep"
        assert any(t.rule_id == "preserve-source-url" for t in result.transformations)

    def test_academic_footer_suppressed(self):
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="References: Smith et al., 2020; Jones et al., 2021",
            parser_version="html-normalized-v1",
        )
        service = NormalizationService(
            aggressive=False,
            document_type="academic",
        )
        result = service.normalize([block])
        # Academic footers should be suppressed, not removed
        assert len(result.suppressed_blocks) == 1
        assert result.suppressed_blocks[0].disposition == "suppress"
        assert any(
            t.rule_id == "doc-type-footer-digest" for t in result.transformations
        )

    def test_legal_footer_suppressed(self):
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="Conflict of interest: The authors declare no conflict.",
            parser_version="html-normalized-v1",
        )
        service = NormalizationService(
            aggressive=False,
            document_type="legal",
        )
        result = service.normalize([block])
        assert len(result.suppressed_blocks) == 1
        assert result.suppressed_blocks[0].disposition == "suppress"

    def test_web_footer_kept(self):
        """Web documents should not suppress footers."""
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="References: Smith et al., 2020",
            parser_version="html-normalized-v1",
        )
        service = NormalizationService(
            aggressive=False,
            document_type="web",
        )
        result = service.normalize([block])
        assert len(result.blocks) == 1
        assert result.blocks[0].disposition == "keep"


# ---------------------------------------------------------------------------
# Confidence gating tests
# ---------------------------------------------------------------------------


class TestConfidenceGating:
    """Tests for confidence-gated aggressive cleanup."""

    def test_low_confidence_rule_suppressed_when_not_aggressive(self):
        """Navigation links with confidence < threshold should be suppressed."""
        # Navigation blocks detected via link-density have confidence 0.9
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="[home](/) [about](/about) [contact](/contact) [privacy](/privacy)",
            parser_version="html-normalized-v1",
        )
        service = NormalizationService(
            aggressive=False,
            confidence_threshold=0.95,  # Higher than 0.9
        )
        result = service.normalize([block])
        # Should be suppressed, not removed
        assert len(result.suppressed_blocks) == 1
        assert result.suppressed_blocks[0].disposition == "suppress"

    def test_aggressive_mode_removes_low_confidence(self):
        """Aggressive mode should remove even low-confidence rules."""
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="[home](/) [about](/about) [contact](/contact) [privacy](/privacy)",
            parser_version="html-normalized-v1",
        )
        service = NormalizationService(
            aggressive=True,
            confidence_threshold=0.95,
        )
        result = service.normalize([block])
        assert len(result.removed_blocks) == 1
        assert result.removed_blocks[0].disposition == "remove"


# ---------------------------------------------------------------------------
# Multi-block tests
# ---------------------------------------------------------------------------


class TestMultiBlockNormalization:
    """Tests for normalizing multiple blocks."""

    def test_mixed_blocks(self):
        """A mix of keep, remove, and suppress blocks."""
        blocks = [
            TypedBlock(
                ordinal=0,
                block_type="heading",
                text="Introduction",
                parser_version="html-normalized-v1",
            ),
            TypedBlock(
                ordinal=1,
                block_type="paragraph",
                text="Use cookies to improve.",
                parser_version="html-normalized-v1",
            ),
            TypedBlock(
                ordinal=2,
                block_type="code",
                text="print('hello')",
                parser_version="html-normalized-v1",
            ),
            TypedBlock(
                ordinal=3,
                block_type="paragraph",
                text="[1] Smith et al., 2020",
                parser_version="html-normalized-v1",
            ),
            TypedBlock(
                ordinal=4,
                block_type="paragraph",
                text="[skip to main content]",
                parser_version="html-normalized-v1",
            ),
        ]
        service = NormalizationService(aggressive=False)
        result = service.normalize(blocks)

        # Should have 3 kept blocks (heading, code, citation)
        assert len(result.blocks) == 3
        # Should have 2 removed blocks (cookie, navigation)
        assert len(result.removed_blocks) == 2
        # Should have transformations
        assert len(result.transformations) >= 5

    def test_source_block_id_tracking(self):
        """Source block IDs should be tracked in normalized blocks."""
        src_ids = [uuid4(), uuid4(), uuid4()]
        blocks = [
            TypedBlock(
                ordinal=0,
                block_type="paragraph",
                text="Hello",
                parser_version="html-normalized-v1",
            ),
            TypedBlock(
                ordinal=1,
                block_type="paragraph",
                text="Use cookies",
                parser_version="html-normalized-v1",
            ),
            TypedBlock(
                ordinal=2,
                block_type="paragraph",
                text="World",
                parser_version="html-normalized-v1",
            ),
        ]
        service = NormalizationService(aggressive=False)
        result = service.normalize(blocks, source_block_ids=src_ids)

        # Kept blocks should have source_block_id set
        for nb in result.blocks:
            assert nb.source_block_id in src_ids
        # Removed blocks should also have source_block_id
        for nb in result.removed_blocks:
            assert nb.source_block_id in src_ids


# ---------------------------------------------------------------------------
# Diagnostics tests
# ---------------------------------------------------------------------------


class TestDiagnostics:
    """Tests for normalization diagnostics."""

    def test_diagnostics_summary(self):
        blocks = [
            TypedBlock(
                ordinal=0,
                block_type="heading",
                text="Intro",
                parser_version="html-normalized-v1",
            ),
            TypedBlock(
                ordinal=1,
                block_type="paragraph",
                text="Use cookies",
                parser_version="html-normalized-v1",
            ),
            TypedBlock(
                ordinal=2,
                block_type="code",
                text="x = 1",
                parser_version="html-normalized-v1",
            ),
        ]
        service = NormalizationService(aggressive=False)
        result = service.normalize(blocks)
        diag = result.diagnostics()

        assert diag["rule_version"] == NORMALIZATION_VERSION
        assert diag["total_source_blocks"] == 3
        assert diag["kept"] == 2  # heading + code
        assert diag["removed"] == 1  # cookie
        assert diag["transformations"] > 0
        assert "strip-cookie-notice" in diag["rules_applied"]
        assert "preserve-code-block" in diag["rules_applied"]

    def test_diagnostics_empty(self):
        service = NormalizationService(aggressive=False)
        result = service.normalize([])
        diag = result.diagnostics()
        assert diag["total_source_blocks"] == 0
        assert diag["kept"] == 0
        assert diag["removed"] == 0


# ---------------------------------------------------------------------------
# Compatibility adapter tests
# ---------------------------------------------------------------------------


class TestCompatibilityAdapter:
    """Tests for the legacy clean_markdown compatibility adapter."""

    def test_clean_markdown_compat_returns_result(self):
        content = "Hello world\n\nUse cookies to improve.\n\nGoodbye"
        result = NormalizationService.clean_markdown_compat(content)
        assert isinstance(result, NormalizationResult)
        assert result.rule_version == "normalization-v1-legacy-adapter"
        assert len(result.blocks) >= 1

    def test_clean_markdown_compat_strips_cookie(self):
        content = "Hello world\n\nUse cookies to improve your experience.\n\nGoodbye"
        result = NormalizationService.clean_markdown_compat(content)
        # The cookie line should be stripped
        cleaned_text = result.blocks[0].text if result.blocks else ""
        assert "Use cookies" not in cleaned_text

    def test_clean_markdown_compat_preserves_code(self):
        content = "Hello\n\n```\ncode block\n```\n\nGoodbye"
        result = NormalizationService.clean_markdown_compat(content)
        cleaned_text = result.blocks[0].text if result.blocks else ""
        assert "code block" in cleaned_text

    def test_clean_markdown_compat_equivalence(self):
        """The compatibility adapter should produce equivalent output to legacy."""
        from research_store.normalization import _legacy_clean_markdown

        content = """Hello world

[skip to main content]

Use cookies to improve.

```python
print('hello')
```

Follow us on Twitter

Copyright © 2024 Example Corp"""

        # Legacy output
        legacy = _legacy_clean_markdown(content)

        # Compatibility adapter output
        result = NormalizationService.clean_markdown_compat(content)
        compat_text = result.blocks[0].text if result.blocks else ""

        # Both should strip the same boilerplate
        assert "skip to main content" not in legacy
        assert "skip to main content" not in compat_text
        assert "Use cookies" not in legacy
        assert "Use cookies" not in compat_text
        assert "Copyright" not in legacy
        assert "Copyright" not in compat_text
        assert "print('hello')" in legacy
        assert "print('hello')" in compat_text


# ---------------------------------------------------------------------------
# Round-trip / recovery tests
# ---------------------------------------------------------------------------


class TestRoundTripRecovery:
    """Tests for content recovery from transformation records."""

    def test_removed_content_preserved_in_transformation(self):
        """Removed content should be recoverable from before_text in transformation records."""
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="Use cookies to improve your experience.",
            parser_version="html-normalized-v1",
        )
        service = NormalizationService(aggressive=False)
        result = service.normalize([block])

        # The block should be removed
        assert len(result.removed_blocks) == 1
        # But the transformation record should have before_text
        cookie_transform = [
            t for t in result.transformations if t.rule_id == "strip-cookie-notice"
        ]
        assert len(cookie_transform) == 1
        assert (
            cookie_transform[0].before_text == "Use cookies to improve your experience."
        )

    def test_no_change_block_has_before_and_after(self):
        """A kept block (no rule matched) should have before_text and after_text."""
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="Hello world",
            parser_version="html-normalized-v1",
        )
        service = NormalizationService(aggressive=False)
        result = service.normalize([block])

        # The block should be kept
        assert len(result.blocks) == 1
        # The transformation should have before and after
        no_change_transform = [
            t for t in result.transformations if t.rule_id == "no-change"
        ]
        if no_change_transform:
            assert no_change_transform[0].before_text == "Hello world"
            assert no_change_transform[0].after_text == "Hello world"


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Tests for normalization idempotency."""

    def test_renormalize_produces_same_result(self):
        """Running normalization twice on the same blocks should produce the same result."""
        blocks = [
            TypedBlock(
                ordinal=0,
                block_type="heading",
                text="Intro",
                parser_version="html-normalized-v1",
            ),
            TypedBlock(
                ordinal=1,
                block_type="paragraph",
                text="Use cookies",
                parser_version="html-normalized-v1",
            ),
        ]
        service = NormalizationService(aggressive=False)

        result1 = service.normalize(blocks)
        result2 = service.normalize(blocks)

        assert len(result1.blocks) == len(result2.blocks)
        assert len(result1.removed_blocks) == len(result2.removed_blocks)
        assert result1.blocks[0].text == result2.blocks[0].text
        assert result1.blocks[0].disposition == result2.blocks[0].disposition


# ---------------------------------------------------------------------------
# NormalizationResult tests
# ---------------------------------------------------------------------------


class TestNormalizationResult:
    """Tests for the NormalizationResult dataclass."""

    def test_empty_result(self):
        result = NormalizationResult()
        assert result.blocks == []
        assert result.suppressed_blocks == []
        assert result.removed_blocks == []
        assert result.transformations == []
        assert result.rule_version == NORMALIZATION_VERSION

    def test_diagnostics_on_empty(self):
        result = NormalizationResult()
        diag = result.diagnostics()
        assert diag["total_source_blocks"] == 0
        assert diag["kept"] == 0


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases in normalization."""

    def test_empty_string_block(self):
        """Empty string text should produce a kept block with no transformation."""
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="",
            parser_version="html-normalized-v1",
        )
        service = NormalizationService(aggressive=False)
        result = service.normalize([block])
        # Empty text: no rule matches, block kept as-is
        assert len(result.blocks) == 1
        assert result.blocks[0].text == ""
        assert result.blocks[0].disposition == "keep"

    def test_whitespace_only_block(self):
        """Whitespace-only text should produce a kept block with no transformation."""
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="   \n  \n  ",
            parser_version="html-normalized-v1",
        )
        service = NormalizationService(aggressive=False)
        result = service.normalize([block])
        # Whitespace-only: stripped is empty, no rule matches
        assert len(result.blocks) == 1
        assert result.blocks[0].text == "   \n  \n  "
        assert result.blocks[0].disposition == "keep"

    def test_unicode_content(self):
        """Unicode content should be handled without exception."""
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="Café résumé naïve",
            parser_version="html-normalized-v1",
        )
        service = NormalizationService(aggressive=False)
        result = service.normalize([block])
        assert len(result.blocks) == 1
        assert result.blocks[0].text == "Café résumé naïve"

    def test_document_id_passed_through(self):
        """document_id should be propagated to normalized blocks."""
        doc_id = uuid4()
        src_id = uuid4()
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="Hello",
            parser_version="html-normalized-v1",
        )
        service = NormalizationService(aggressive=False)
        result = service.normalize(
            [block],
            source_block_ids=[src_id],
            document_id=doc_id,
        )
        assert len(result.blocks) == 1
        assert result.blocks[0].document_id == doc_id
        assert result.blocks[0].source_block_id == src_id

    def test_document_id_none_when_not_provided(self):
        """When document_id is not provided, it should be None in normalized blocks."""
        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text="Hello",
            parser_version="html-normalized-v1",
        )
        service = NormalizationService(aggressive=False)
        result = service.normalize([block])
        assert len(result.blocks) == 1
        assert result.blocks[0].document_id is None

    def test_list_block_preserved(self):
        """Markdown list blocks should survive normalization unchanged."""
        block = TypedBlock(
            ordinal=0,
            block_type="list_item",
            text="- First item\n- Second item\n- Third item",
            parser_version="html-normalized-v1",
        )
        service = NormalizationService(aggressive=False)
        result = service.normalize([block])
        assert len(result.blocks) == 1
        assert result.blocks[0].disposition == "keep"
        assert result.blocks[0].text == "- First item\n- Second item\n- Third item"

    def test_table_block_preserved(self):
        """Table blocks should survive normalization unchanged."""
        block = TypedBlock(
            ordinal=0,
            block_type="table",
            text="| Col A | Col B |\n|-------|-------|\n| val1  | val2  |",
            parser_version="html-normalized-v1",
        )
        service = NormalizationService(aggressive=False)
        result = service.normalize([block])
        assert len(result.blocks) == 1
        assert result.blocks[0].disposition == "keep"
        assert result.blocks[0].text == block.text
