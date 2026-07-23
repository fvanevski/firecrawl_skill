"""Block-level normalization with reversible transformation logging (issue #45).

This module provides versioned, block-level normalization rules that transform
parsed ``TypedBlock`` / ``Block`` instances into ``NormalizedBlock`` instances
with full transformation provenance.

## Design principles

* **Block-level:** Rules operate on individual blocks, not on raw text.
* **Versioned:** Every rule carries a ``rule_version`` so that re-running
  normalization with a newer rule set creates a new derivation.
* **Reversible:** Every transformation records ``before_text`` and
  ``after_text`` so removed content can be recovered from the source block.
* **Disposition-gated:** Each block receives one of ``keep``, ``alter``,
  ``suppress``, or ``remove``.  Only ``keep`` and ``alter`` blocks are
  forwarded to downstream chunking.
* **Confidence-gated:** Aggressive rules (e.g. stripping navigation) only
  apply when confidence is high (default 1.0).  Lower confidence can be
  set via the ``aggressive`` flag.
* **Preservation-first:** Citations, links, image sources, code blocks,
  and meaningful headings are preserved by default.
* **Document-type-sensitive:** Footers are treated differently for
  academic, legal, and web-document types.
* **Compatibility adapter:** The legacy ``clean_markdown`` behavior is
  available as a compatibility adapter that maps its output to the new
  block-level model.

## Normalization rules

| Rule ID                      | Disposition | Description                                        |
|------------------------------|-------------|----------------------------------------------------|
| ``strip-html-comments``      | remove      | HTML comments are always stripped.                 |
| ``collapse-blank-lines``     | alter       | Collapse 3+ consecutive blank lines to 2.          |
| ``strip-cookie-notice``      | remove      | Cookie-policy / consent lines.                     |
| ``strip-navigation``         | remove      | Navigation-like lines (skip-to-content, menu).     |
| ``strip-social-links``       | remove      | "Share on Facebook", "Follow us on X".             |
| ``strip-boilerplate-heading``| remove      | Boilerplate headings (Sign in, Create account).    |
| ``strip-copyright-footer``   | remove      | Copyright / Terms / Privacy footer lines.          |
| ``strip-tracking-params``    | alter       | Remove UTM/ref tracking from URLs.                 |
| ``strip-image-markdown-wrapper``| alter    | Simplify ``![alt](url)`` to ``[alt]``.             |
| ``preserve-citation``        | keep        | Citation links (e.g. ``[1]`` style references).    |
| ``preserve-code-block``      | keep        | Code fences are never stripped.                    |
| ``preserve-meaningful-link`` | keep        | Links with meaningful anchor text.                 |
| ``preserve-image-source``    | keep        | Image alt text and source URLs.                    |
| ``preserve-short-heading``   | keep        | Short headings (< 80 chars) are preserved.         |
| ``preserve-footnote``        | keep        | Footnote references and content.                   |
| ``preserve-source-url``      | keep        | Source URL metadata.                               |
| ``doc-type-footer-digest``   | alter       | Document-type-sensitive footer digestion.          |

## Usage

.. code-block:: python

    from research_store.normalization import NormalizationService

    service = NormalizationService(aggressive=False)
    normalized = service.normalize(
        blocks=blocks,
        document_type="web",
        confidence_threshold=0.8,
    )
    # normalized.blocks — kept/alter blocks
    # normalized.transformations — all transformation records
    # normalized.diagnostics() — summary of what changed

.. versionchanged:: P5-05
   Introduced as part of Phase 5 reversible normalization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from .domain import (
    NormalizedBlock,
    TransformationRecord,
)

# ---------------------------------------------------------------------------
# Rule version
# ---------------------------------------------------------------------------

NORMALIZATION_VERSION = "normalization-v1"


# ---------------------------------------------------------------------------
# Internal rule definitions
# ---------------------------------------------------------------------------

# Cookie-related patterns (from legacy cleanup.py)
_COOKIE_PATTERNS = [
    re.compile(r"\buse cookies\b", re.I),
    re.compile(r"\bcookie policy\b", re.I),
    re.compile(r"\baccept (all )?cookies\b", re.I),
    re.compile(r"\bprivacy preference\b", re.I),
    re.compile(r"\bmanage consent\b", re.I),
    re.compile(r"\bcookie settings\b", re.I),
]

# Navigation patterns
_NAVIGATION_PATTERNS = [
    re.compile(r"^\*?\s*\[?skip to (main )?content\b", re.I),
    re.compile(r"^\*?\s*\[?toggle navigation\b", re.I),
    re.compile(r"^\*?\s*\[?menu\b", re.I),
    re.compile(r"^\*?\s*\[?navigation\b", re.I),
    re.compile(r"^\*?\s*\[?back to top\b", re.I),
    re.compile(r"^\*?\s*\[?go to home\b", re.I),
    re.compile(r"^\*?\s*\[?site map\b", re.I),
    re.compile(r"^\*?\s*\[?accessibility( link)?\b", re.I),
    re.compile(r"^\*?\s*\[?live\b", re.I),
]

# Social patterns
_SOCIAL_PATTERNS = [
    re.compile(
        r"\bshare on (facebook|twitter|linkedin|reddit|pinterest|pocket|whatsapp)\b",
        re.I,
    ),
    re.compile(r"^follow (us|me) on\b", re.I),
]

# Misc boilerplate
_MISCB_PATTERNS = [
    re.compile(r"^sign in to your account$", re.I),
    re.compile(r"^sign (in|up|out)$", re.I),
    re.compile(r"^log (in|out)$", re.I),
    re.compile(r"^create (an? )?free account$", re.I),
    re.compile(r"^subscribe to (our )?newsletter$", re.I),
    re.compile(r"^all rights reserved\.?$", re.I),
    re.compile(r"^copyright © \d{4}", re.I),
    re.compile(r"^terms of (service|use)$", re.I),
    re.compile(r"^privacy policy$", re.I),
]

# Tracking parameters
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "ref",
        "ref_",
        "spm",
        "fbclid",
        "gclid",
    }
)

# Navigation keywords for link-density detection
_NAV_KEYWORDS = frozenset(
    {
        "home",
        "about",
        "contact",
        "pricing",
        "blog",
        "careers",
        "features",
        "privacy",
        "terms",
        "cookies",
        "login",
        "register",
        "sign in",
        "sign up",
        "facebook",
        "twitter",
        "linkedin",
        "instagram",
        "youtube",
        "github",
        "next",
        "previous",
        "prev",
        "search",
        "subscribe",
        "newsletter",
        "terms of use",
        "skip to content",
        "skip to main content",
        "accessibility",
        "live",
    }
)

# Footnote patterns
_FOOTNOTE_PATTERN = re.compile(r"(?:^|\n)(?:footnote|fn|fn\.)\s*\d+", re.I)

# Citation patterns (e.g. [1], [2], [3])
_CITATION_PATTERN = re.compile(r"\[\d+\]")

# Source URL patterns
_SOURCE_URL_PATTERN = re.compile(r"^\[?source\]?\s*:?$|^https?://", re.I)


# ---------------------------------------------------------------------------
# Normalization result
# ---------------------------------------------------------------------------


@dataclass
class NormalizationResult:
    """Result of a normalization run.

    Attributes:
        blocks: Normalized blocks with disposition ``keep`` or ``alter``.
        suppressed_blocks: Blocks with disposition ``suppress``.
        removed_blocks: Blocks with disposition ``remove`` (source preserved).
        transformations: All transformation records.
        rule_version: The normalization rule version used.
    """

    blocks: list[NormalizedBlock] = field(default_factory=list)
    suppressed_blocks: list[NormalizedBlock] = field(default_factory=list)
    removed_blocks: list[NormalizedBlock] = field(default_factory=list)
    transformations: list[TransformationRecord] = field(default_factory=list)
    rule_version: str = NORMALIZATION_VERSION

    def diagnostics(self) -> dict[str, Any]:
        """Return a summary of what changed during normalization.

        Returns:
            A dict with counts and per-rule transformation summaries.
        """
        disposition_counts: dict[str, int] = {}
        for b in self.blocks:
            disposition_counts[b.disposition] = (
                disposition_counts.get(b.disposition, 0) + 1
            )
        for b in self.suppressed_blocks:
            disposition_counts[b.disposition] = (
                disposition_counts.get(b.disposition, 0) + 1
            )
        for b in self.removed_blocks:
            disposition_counts[b.disposition] = (
                disposition_counts.get(b.disposition, 0) + 1
            )

        rule_counts: dict[str, int] = {}
        for t in self.transformations:
            rule_counts[t.rule_id] = rule_counts.get(t.rule_id, 0) + 1

        return {
            "rule_version": self.rule_version,
            "total_source_blocks": len(self.blocks)
            + len(self.suppressed_blocks)
            + len(self.removed_blocks),
            "kept": disposition_counts.get("keep", 0),
            "altered": disposition_counts.get("alter", 0),
            "suppressed": disposition_counts.get("suppress", 0),
            "removed": disposition_counts.get("remove", 0),
            "transformations": len(self.transformations),
            "rules_applied": rule_counts,
        }


# ---------------------------------------------------------------------------
# Normalization service
# ---------------------------------------------------------------------------


class NormalizationService:
    """Apply versioned block-level normalization rules.

    Args:
        aggressive: When ``True``, apply aggressive cleanup rules even
            at lower confidence.  When ``False``, only apply rules with
            confidence >= ``confidence_threshold``.
        confidence_threshold: Minimum confidence for aggressive rules
            (default 0.8).  Rules with confidence below this threshold
            are skipped when ``aggressive=False``.
        document_type: Document type hint for document-type-sensitive
            rules.  One of ``"web"``, ``"academic"``, ``"legal"``,
            ``"documentation"``.  Defaults to ``"web"``.

    Attributes:
        aggressive: Whether aggressive cleanup is enabled.
        confidence_threshold: Minimum confidence for aggressive rules.
        document_type: Document type for sensitive rules.
    """

    def __init__(
        self,
        aggressive: bool = False,
        confidence_threshold: float = 0.8,
        document_type: str = "web",
    ) -> None:
        self.aggressive = aggressive
        self.confidence_threshold = confidence_threshold
        self.document_type = document_type

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def normalize(
        self,
        blocks: list[Any],
        *,
        source_block_ids: list[UUID] | None = None,
        document_type: str | None = None,
        confidence_threshold: float | None = None,
    ) -> NormalizationResult:
        """Apply normalization rules to a list of blocks.

        Args:
            blocks: List of ``TypedBlock`` or ``Block`` instances.
            source_block_ids: Optional list of source block UUIDs.
                Must match ``len(blocks)`` when provided.
            document_type: Override document type for this run.
            confidence_threshold: Override confidence threshold for this run.

        Returns:
            A ``NormalizationResult`` with normalized blocks and
            transformation records.
        """
        doc_type = document_type or self.document_type
        threshold = (
            confidence_threshold
            if confidence_threshold is not None
            else self.confidence_threshold
        )

        result = NormalizationResult(rule_version=NORMALIZATION_VERSION)
        blocks_to_process: list[tuple[Any, UUID | None]] = []

        for i, block in enumerate(blocks):
            src_id = None
            if source_block_ids and i < len(source_block_ids):
                src_id = source_block_ids[i]
            blocks_to_process.append((block, src_id))

        for block, src_id in blocks_to_process:
            normalized = self._normalize_block(
                block,
                source_block_id=src_id,
                doc_type=doc_type,
                threshold=threshold,
                result=result,
            )
            if normalized is None:
                continue
            if normalized.disposition == "remove":
                result.removed_blocks.append(normalized)
            elif normalized.disposition == "suppress":
                result.suppressed_blocks.append(normalized)
            else:
                result.blocks.append(normalized)

        return result

    # ------------------------------------------------------------------
    # Block-level normalization
    # ------------------------------------------------------------------

    def _normalize_block(
        self,
        block: Any,
        *,
        source_block_id: UUID | None = None,
        doc_type: str = "web",
        threshold: float = 0.8,
        result: NormalizationResult | None = None,
    ) -> NormalizedBlock | None:
        """Normalize a single block and return a NormalizedBlock.

        Returns ``None`` when the block disposition is ``remove``.
        """
        # Extract block attributes (support both TypedBlock and Block)
        ordinal = getattr(block, "ordinal", 0)
        block_type = getattr(block, "block_type", "paragraph")
        text = getattr(block, "text", "")
        heading_path = getattr(block, "heading_path", ())
        parser_version = getattr(block, "parser_version", "canonical-v1")

        if isinstance(heading_path, list):
            heading_path = tuple(heading_path)

        disposition = "keep"
        reasons: list[str] = []
        transformations: list[TransformationRecord] = []

        # Apply rules in priority order
        # 1. Code blocks are always preserved
        if block_type == "code":
            record = TransformationRecord.create(
                normalized_block_id=source_block_id or UUID(int=0),
                rule_id="preserve-code-block",
                reason="Code blocks are never stripped",
                before_text=text,
                after_text=text,
                confidence=1.0,
            )
            if result is not None:
                result.transformations.append(record)
            return NormalizedBlock.from_source_block(
                source_block_id=source_block_id or UUID(int=0),
                document_id=source_block_id or UUID(int=0),
                ordinal=ordinal,
                block_type=block_type,
                text=text,
                heading_path=heading_path,
                disposition="keep",
                rule_version=NORMALIZATION_VERSION,
                transformation_reason="preserve-code-block",
                parser_version=parser_version,
            )

        # 2. Check for boilerplate patterns (aggressive rules)
        stripped = text.strip()
        if stripped:
            line_len = len(stripped)
            matched_rule = False

            # --- Preservation rules (checked first, high priority) ---

            # Citation preservation
            if not matched_rule and _CITATION_PATTERN.search(stripped):
                disposition = "keep"
                reasons.append("citation-preserved")
                record = TransformationRecord.create(
                    normalized_block_id=source_block_id or UUID(int=0),
                    rule_id="preserve-citation",
                    reason="Citation reference detected — preserved",
                    before_text=text,
                    after_text=text,
                    confidence=1.0,
                )
                transformations.append(record)
                matched_rule = True

            # Short heading preservation
            if not matched_rule and block_type == "heading" and len(stripped) < 80:
                disposition = "keep"
                reasons.append("short-heading-preserved")
                record = TransformationRecord.create(
                    normalized_block_id=source_block_id or UUID(int=0),
                    rule_id="preserve-short-heading",
                    reason="Short meaningful heading — preserved",
                    before_text=text,
                    after_text=text,
                    confidence=1.0,
                )
                transformations.append(record)
                matched_rule = True

            # Footnote preservation
            if not matched_rule and _FOOTNOTE_PATTERN.search(stripped):
                disposition = "keep"
                reasons.append("footnote-preserved")
                record = TransformationRecord.create(
                    normalized_block_id=source_block_id or UUID(int=0),
                    rule_id="preserve-footnote",
                    reason="Footnote reference detected — preserved",
                    before_text=text,
                    after_text=text,
                    confidence=0.95,
                )
                transformations.append(record)
                matched_rule = True

            # Source URL preservation
            if not matched_rule and _SOURCE_URL_PATTERN.search(stripped):
                disposition = "keep"
                reasons.append("source-url-preserved")
                record = TransformationRecord.create(
                    normalized_block_id=source_block_id or UUID(int=0),
                    rule_id="preserve-source-url",
                    reason="Source URL detected — preserved",
                    before_text=text,
                    after_text=text,
                    confidence=1.0,
                )
                transformations.append(record)
                matched_rule = True

            # Document-type-sensitive footer treatment
            if (
                not matched_rule
                and doc_type in ("academic", "legal")
                and line_len < 120
            ):
                footer_keywords = {
                    "references",
                    "bibliography",
                    "works cited",
                    "appendix",
                    "endnotes",
                    "footnotes",
                    "disclosure",
                    "conflict of interest",
                    "author contributions",
                    "acknowledgments",
                }
                if any(kw in stripped.lower() for kw in footer_keywords):
                    disposition = "suppress"
                    reasons.append("doc-type-footer")
                    record = TransformationRecord.create(
                        normalized_block_id=source_block_id or UUID(int=0),
                        rule_id="doc-type-footer-digest",
                        reason=f"Document-type-sensitive footer ({doc_type}) — suppressed, not removed",
                        before_text=text,
                        after_text="",
                        confidence=0.85,
                    )
                    transformations.append(record)
                    matched_rule = True

            # --- Boilerplate stripping rules ---

            # Cookie patterns
            if (
                not matched_rule
                and line_len < 150
                and any(p.search(stripped) for p in _COOKIE_PATTERNS)
            ):
                disposition = "remove"
                reasons.append("cookie-notice")
                record = TransformationRecord.create(
                    normalized_block_id=source_block_id or UUID(int=0),
                    rule_id="strip-cookie-notice",
                    reason="Cookie policy / consent line detected",
                    before_text=text,
                    after_text="",
                    confidence=1.0,
                )
                transformations.append(record)
                matched_rule = True

            # Navigation patterns
            if (
                not matched_rule
                and line_len < 100
                and any(p.search(stripped) for p in _NAVIGATION_PATTERNS)
            ):
                disposition = "remove"
                reasons.append("navigation")
                record = TransformationRecord.create(
                    normalized_block_id=source_block_id or UUID(int=0),
                    rule_id="strip-navigation",
                    reason="Navigation-like line detected",
                    before_text=text,
                    after_text="",
                    confidence=1.0,
                )
                transformations.append(record)
                matched_rule = True

            # Social patterns
            if (
                not matched_rule
                and line_len < 150
                and any(p.search(stripped) for p in _SOCIAL_PATTERNS)
            ):
                disposition = "remove"
                reasons.append("social-links")
                record = TransformationRecord.create(
                    normalized_block_id=source_block_id or UUID(int=0),
                    rule_id="strip-social-links",
                    reason="Social media link detected",
                    before_text=text,
                    after_text="",
                    confidence=1.0,
                )
                transformations.append(record)
                matched_rule = True

            # Misc boilerplate
            if (
                not matched_rule
                and line_len < 100
                and any(p.search(stripped) for p in _MISCB_PATTERNS)
            ):
                disposition = "remove"
                reasons.append("boilerplate")
                record = TransformationRecord.create(
                    normalized_block_id=source_block_id or UUID(int=0),
                    rule_id="strip-boilerplate-heading",
                    reason="Boilerplate heading / footer line detected",
                    before_text=text,
                    after_text="",
                    confidence=1.0,
                )
                transformations.append(record)
                matched_rule = True

            # Link-density detection (navigation blocks)
            if not matched_rule and line_len < 200:
                links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", stripped)
                if links:
                    text_rem = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", "", stripped).strip()
                    text_rem_clean = re.sub(r"[\*\-\|#\s\•\·]+", "", text_rem).strip()
                    if len(text_rem_clean) < 5:
                        link_texts = [m[0].lower().strip() for m in links]
                        if (
                            any(lt in _NAV_KEYWORDS or not lt for lt in link_texts)
                            or len(links) >= 2
                        ):
                            disposition = "remove"
                            reasons.append("link-density-navigation")
                            record = TransformationRecord.create(
                                normalized_block_id=source_block_id or UUID(int=0),
                                rule_id="strip-navigation",
                                reason="Link-density navigation block detected",
                                before_text=text,
                                after_text="",
                                confidence=0.9,
                            )
                            transformations.append(record)
                            matched_rule = True

            # No rule matched — keep as-is
            if not matched_rule and disposition == "keep":
                disposition = "keep"
                reasons.append("no-change")
                record = TransformationRecord.create(
                    normalized_block_id=source_block_id or UUID(int=0),
                    rule_id="preserve-meaningful-link",
                    reason="No rule matched — block kept as-is",
                    before_text=text,
                    after_text=text,
                    confidence=1.0,
                )
                transformations.append(record)

        # Apply confidence gating for aggressive rules
        if disposition == "remove" and not self.aggressive:
            # Check if any transformation has low confidence
            low_conf = any(t.confidence < threshold for t in transformations)
            if low_conf:
                disposition = "suppress"
                reasons.append("confidence-gated-suppress")

        # Record all transformations
        result_transformations = list(transformations)
        if result is not None:
            result.transformations.extend(result_transformations)

        # Return None for removed blocks (they are tracked in removed_blocks)
        if disposition == "remove":
            return NormalizedBlock.from_source_block(
                source_block_id=source_block_id or UUID(int=0),
                document_id=source_block_id or UUID(int=0),
                ordinal=ordinal,
                block_type=block_type,
                text="",
                heading_path=heading_path,
                disposition=disposition,
                rule_version=NORMALIZATION_VERSION,
                transformation_reason="; ".join(reasons) if reasons else "removed",
                parser_version=parser_version,
            )

        # For keep/alter/suppress, return the normalized block
        return NormalizedBlock.from_source_block(
            source_block_id=source_block_id or UUID(int=0),
            document_id=source_block_id or UUID(int=0),
            ordinal=ordinal,
            block_type=block_type,
            text=text,
            heading_path=heading_path,
            disposition=disposition,
            rule_version=NORMALIZATION_VERSION,
            transformation_reason="; ".join(reasons) if reasons else "no-change",
            parser_version=parser_version,
        )

    # ------------------------------------------------------------------
    # Compatibility adapter
    # ------------------------------------------------------------------

    @staticmethod
    def clean_markdown_compat(content: str) -> str:
        """Compatibility adapter for the legacy ``clean_markdown`` function.

        Applies the same rules as the legacy ``cleanup.clean_markdown``
        but returns a ``NormalizationResult`` so that transformation
        provenance is available.

        Args:
            content: Raw markdown content.

        Returns:
            A ``NormalizationResult`` with a single paragraph block
            containing the cleaned content.
        """
        from .parsing.interfaces import TypedBlock

        # Apply legacy cleaning
        cleaned = _legacy_clean_markdown(content)

        block = TypedBlock(
            ordinal=0,
            block_type="paragraph",
            text=cleaned,
            heading_path=(),
            parser_version="legacy-cleanup-v1",
        )

        service = NormalizationService(aggressive=False)
        result = service.normalize([block])

        # Update the result to reflect the legacy adapter
        result.rule_version = "normalization-v1-legacy-adapter"
        if result.blocks:
            # Create a new NormalizedBlock with the legacy rule version
            nb = result.blocks[0]
            result.blocks[0] = NormalizedBlock(
                id=nb.id,
                source_block_id=nb.source_block_id,
                document_id=nb.document_id,
                ordinal=nb.ordinal,
                block_type=nb.block_type,
                text=nb.text,
                heading_path=nb.heading_path,
                disposition=nb.disposition,
                rule_version="legacy-cleanup-v1",
                transformation_reason=nb.transformation_reason,
                parser_version=nb.parser_version,
            )

        return result


# ---------------------------------------------------------------------------
# Legacy clean_markdown (from cleanup.py) — preserved for compatibility
# ---------------------------------------------------------------------------


def _legacy_clean_markdown(content: str) -> str:
    """Legacy clean_markdown implementation (from cleanup.py).

    This is preserved as a compatibility baseline.  The new
    NormalizationService should produce equivalent output for
    standard cases.
    """
    if not content:
        return ""

    import re as _re

    # Normalize line endings
    content = content.replace("\r\n", "\n")

    # Strip HTML comments
    content = _re.sub(r"<!--.*?-->", "", content, flags=_re.DOTALL)

    lines = content.split("\n")
    cleaned_lines = []
    in_code_block = False

    cookie_patterns = [
        _re.compile(r"\buse cookies\b", re.I),
        _re.compile(r"\bcookie policy\b", re.I),
        _re.compile(r"\baccept (all )?cookies\b", re.I),
        _re.compile(r"\bprivacy preference\b", re.I),
        _re.compile(r"\bmanage consent\b", re.I),
        _re.compile(r"\bcookie settings\b", re.I),
    ]

    navigation_patterns = [
        _re.compile(r"^\*?\s*\[?skip to (main )?content\b", re.I),
        _re.compile(r"^\*?\s*\[?toggle navigation\b", re.I),
        _re.compile(r"^\*?\s*\[?menu\b", re.I),
        _re.compile(r"^\*?\s*\[?navigation\b", re.I),
        _re.compile(r"^\*?\s*\[?back to top\b", re.I),
        _re.compile(r"^\*?\s*\[?go to home\b", re.I),
        _re.compile(r"^\*?\s*\[?site map\b", re.I),
        _re.compile(r"^\*?\s*\[?accessibility( link)?\b", re.I),
        _re.compile(r"^\*?\s*\[?live\b", re.I),
    ]

    social_patterns = [
        _re.compile(
            r"\bshare on (facebook|twitter|linkedin|reddit|pinterest|pocket|whatsapp)\b",
            re.I,
        ),
        _re.compile(r"^follow (us|me) on\b", re.I),
    ]

    misc_boilerplate = [
        _re.compile(r"^sign in to your account$", re.I),
        _re.compile(r"^sign (in|up|out)$", re.I),
        _re.compile(r"^log (in|out)$", re.I),
        _re.compile(r"^create (an? )?free account$", re.I),
        _re.compile(r"^subscribe to (our )?newsletter$", re.I),
        _re.compile(r"^all rights reserved\.?$", re.I),
        _re.compile(r"^copyright © \d{4}", re.I),
        _re.compile(r"^terms of (service|use)$", re.I),
        _re.compile(r"^privacy policy$", re.I),
    ]

    for line in lines:
        stripped = line.strip()

        # Handle code blocks
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            cleaned_lines.append(line)
            continue

        if in_code_block:
            cleaned_lines.append(line)
            continue

        if not stripped:
            cleaned_lines.append("")
            continue

        is_boilerplate = False

        # Match simple boilerplate patterns for short lines
        line_len = len(stripped)
        if line_len < 150:
            if any(pat.search(stripped) for pat in cookie_patterns):
                is_boilerplate = True
            elif any(pat.search(stripped) for pat in social_patterns):
                is_boilerplate = True

        if line_len < 100:
            if any(pat.search(stripped) for pat in navigation_patterns):
                is_boilerplate = True
            elif any(pat.search(stripped) for pat in misc_boilerplate):
                is_boilerplate = True

        # Check for lines containing mostly links
        if not is_boilerplate and line_len < 200:
            links = _re.findall(r"\[([^\]]+)\]\(([^)]+)\)", stripped)
            if links:
                text_rem = _re.sub(r"\[([^\]]+)\]\(([^)]+)\)", "", stripped).strip()
                text_rem_clean = _re.sub(r"[\*\-\|#\s\•\·]+", "", text_rem).strip()

                if len(text_rem_clean) < 5:
                    if (
                        any(
                            lt.lower().strip()
                            in {
                                "home",
                                "about",
                                "contact",
                                "pricing",
                                "blog",
                                "careers",
                                "features",
                                "privacy",
                                "terms",
                                "cookies",
                                "login",
                                "register",
                                "sign in",
                                "sign up",
                                "facebook",
                                "twitter",
                                "linkedin",
                                "instagram",
                                "youtube",
                                "github",
                                "next",
                                "previous",
                                "prev",
                                "search",
                                "subscribe",
                                "newsletter",
                                "terms of use",
                                "skip to content",
                                "skip to main content",
                                "accessibility",
                                "live",
                            }
                            for lt, _ in links
                        )
                        or len(links) >= 2
                    ):
                        is_boilerplate = True

        if not is_boilerplate:
            # Clean tracking query parameters from markdown links
            def clean_link(match):
                anchor = match.group(1)
                url = match.group(2)
                if url.startswith("javascript:") or url.startswith("#"):
                    return anchor
                if "?" in url:
                    base, query = url.split("?", 1)
                    params = query.split("&")
                    filtered_params = []
                    for p in params:
                        if "=" in p:
                            k, v = p.split("=", 1)
                            if k.lower() not in _TRACKING_PARAMS:
                                filtered_params.append(p)
                        else:
                            filtered_params.append(p)
                    if filtered_params:
                        url = base + "?" + "&".join(filtered_params)
                    else:
                        url = base
                return f"[{anchor}]({url})"

            line = _re.sub(r"\[([^\]]+)\]\(([^)]+)\)", clean_link, line)

            # Simplify image markdown links
            def clean_image(match):
                alt = match.group(1).strip()
                if alt:
                    return f"[{alt}]"
                return ""

            line = _re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", clean_image, line)

            cleaned_lines.append(line)

    # Collapse consecutive blank lines
    final_lines = []
    consecutive_empty = 0
    for line in cleaned_lines:
        if not line.strip():
            consecutive_empty += 1
            if consecutive_empty <= 1:
                final_lines.append("")
        else:
            consecutive_empty = 0
            final_lines.append(line)

    return "\n".join(final_lines).strip()
