"""Deterministic extraction-quality evaluator.

Computes a versioned quality vector from raw extraction content bytes
and optional metadata.  The evaluator is pure and deterministic —
given the same inputs it always produces the same output.

## Signals computed

* **Length** — byte length, visible text length.
* **Structure** — heading count, paragraph count, list count, table
  count, code-to-prose ratio.
* **Density** — link density, boilerplate ratio.
* **Content** — title presence, MIME consistency, language confidence,
  extraction method confidence.
* **Signals** — anti-bot markers, duplicate content similarity,
  query-term coverage, parser warnings, required structured fields.
* **Anomalies** — encoding anomalies detected during decoding.

## Versioning

Every evaluation carries a ``quality_version`` (default ``quality-v1``).
Upgrading the evaluator creates new rows with the new version — old
evaluations are never mutated.

## Invariants

* Content length alone never determines success.
* Anti-bot markers are a hard-fail regardless of length.
* Ambiguous cases are identifiable for semantic adjudication.
"""

from __future__ import annotations

import html
import re
from typing import Iterable

from .domain import ExtractionQualityMetrics
from .quality_config import QualityConfig


def evaluate_quality(
    content: bytes,
    *,
    mime_type: str | None = None,
    title: str | None = None,
    query_terms: Iterable[str] | None = None,
    config: QualityConfig | None = None,
    duplicate_similarity: float = 0.0,
    expected_mime_type: str | None = None,
) -> ExtractionQualityMetrics:
    """Compute deterministic quality metrics from raw extraction content.

    Args:
        content: Raw extraction output bytes (HTML, Markdown, JSON, etc.).
        mime_type: Declared MIME type from the extraction backend.
        title: Document title if available.
        query_terms: Expected query terms for relevance scoring.
        config: Quality thresholds.  Defaults to
            ``QualityConfig.from_env()``.
        duplicate_similarity: Pre-computed duplicate similarity score
            (0.0 – 1.0).  Pass 0.0 when not available.
        expected_mime_type: Expected MIME type for consistency check.

    Returns:
        ``ExtractionQualityMetrics`` with all signal fields populated.
    """
    if config is None:
        config = QualityConfig.from_env()

    # Decode content for analysis
    visible_text, encoding_anomaly = _decode_visible(content)

    # Compute each signal
    byte_length = len(content)
    visible_text_length = len(visible_text)
    paragraph_count = _count_paragraphs(visible_text)
    heading_count = _count_headings(visible_text)
    list_count = _count_lists(visible_text)
    table_count = _count_tables(visible_text)
    link_density = _compute_link_density(visible_text)
    boilerplate_ratio = _compute_boilerplate_ratio(visible_text)
    title_present = _check_title_present(visible_text, title)
    content_type_consistent = _check_content_type_consistency(
        mime_type, expected_mime_type, visible_text, content
    )
    anti_bot_markers = _count_anti_bot_markers(visible_text)
    language_confidence = _compute_language_confidence(visible_text)
    query_term_coverage = _compute_query_term_coverage(visible_text, query_terms)
    extraction_method_confidence = _compute_extraction_confidence(
        visible_text_length, heading_count, paragraph_count
    )
    code_to_prose_ratio = _compute_code_to_prose_ratio(visible_text)
    parser_warnings = 0  # Populated by the parser; evaluator starts at 0

    return ExtractionQualityMetrics(
        byte_length=byte_length,
        visible_text_length=visible_text_length,
        paragraph_count=paragraph_count,
        heading_count=heading_count,
        list_count=list_count,
        table_count=table_count,
        link_density=round(link_density, 4),
        boilerplate_ratio=round(boilerplate_ratio, 4),
        title_present=title_present,
        language_confidence=round(language_confidence, 4),
        content_type_consistent=content_type_consistent,
        anti_bot_markers=anti_bot_markers,
        duplicate_content_similarity=duplicate_similarity,
        query_term_coverage=round(query_term_coverage, 4),
        required_structured_fields=_count_required_structured(
            heading_count, table_count, list_count
        ),
        parser_warnings=parser_warnings,
        code_to_prose_ratio=round(code_to_prose_ratio, 4),
        extraction_method_confidence=round(extraction_method_confidence, 4),
        quality_version=config.quality_version,
    )


# -----------------------------------------------------------------------
# Signal computation functions
# -----------------------------------------------------------------------


def _decode_visible(content: bytes) -> tuple[str, bool]:
    """Decode bytes to visible text and detect encoding anomalies.

    Returns:
        A tuple of (decoded text, encoding_anomaly bool).
    """
    encoding_anomaly = False
    for encoding in ("utf-8", "latin-1"):
        try:
            text = content.decode(encoding)
            if encoding == "latin-1":
                # latin-1 always succeeds — flag if non-ASCII present
                encoding_anomaly = any(ord(c) > 127 for c in text)
            # Strip HTML tags for visible text
            text = _strip_html_tags(text)
            text = html.unescape(text)
            # Strip leading/trailing whitespace and collapse internal
            text = "\n".join(line.rstrip() for line in text.split("\n"))
            # Strip all-whitespace lines
            text = "\n".join(line for line in text.split("\n") if line.strip())
            return text, encoding_anomaly
        except (UnicodeDecodeError, ValueError):
            continue
    # Fallback: errors='replace'
    text = content.decode("utf-8", errors="replace")
    text = _strip_html_tags(text)
    return text, True


def _strip_html_tags(text: str) -> str:
    """Remove HTML tags but preserve whitespace structure."""
    return re.sub(r"<[^>]*>", " ", text)


def _count_paragraphs(text: str) -> int:
    """Count paragraphs — blocks of text separated by blank lines or headings.

    Headings act as paragraph boundaries since they introduce new sections.
    """
    # Split on headings first
    parts = re.split(r"^#{1,6}\s", text, flags=re.MULTILINE)
    total = 0
    for part in parts:
        total += _count_paragraphs_in_block(part)
    return total


def _count_paragraphs_in_block(text: str) -> int:
    """Count paragraphs within a single block (between headings)."""
    lines = text.split("\n")
    paragraphs = 0
    current_block = []
    for line in lines:
        stripped = line.strip()
        if stripped:
            current_block.append(stripped)
        else:
            if current_block:
                paragraphs += 1
                current_block = []
    if current_block:
        paragraphs += 1
    return paragraphs


def _count_headings(text: str) -> int:
    """Count ATX-style headings (# ...) and HTML heading tags."""
    atx = len(re.findall(r"^#{1,6}\s", text, re.MULTILINE))
    html_tags = len(re.findall(r"<h[1-6][\s>]", text, re.IGNORECASE))
    return max(atx, html_tags)


def _count_lists(text: str) -> int:
    """Count unordered and ordered list items."""
    unordered = len(re.findall(r"^\s*[-*+]\s", text, re.MULTILINE))
    ordered = len(re.findall(r"^\s*\d+\.\s", text, re.MULTILINE))
    html_items = len(re.findall(r"<li[\s>]", text, re.IGNORECASE))
    return max(unordered + ordered, html_items)


def _count_tables(text: str) -> int:
    """Count Markdown pipe tables and HTML table elements.

    Markdown pipe tables require a header row followed by a separator
    row (containing only dashes and pipes).
    """
    lines = text.split("\n")
    # Count HTML table elements
    html_tables = len(re.findall(r"<table[\s>]", text, re.IGNORECASE))

    # Count Markdown pipe tables: look for header + separator pattern
    markdown_tables = 0
    i = 0
    while i < len(lines) - 1:
        # Check for header line (has | and non-pipe content)
        if "|" in lines[i]:
            # Check for separator line (only dashes, pipes, spaces)
            if i + 1 < len(lines):
                sep = lines[i + 1].strip()
                if sep and re.match(r"^[\s|:-]+$", sep) and "-" in sep:
                    markdown_tables += 1
                    i += 2
                    continue
        i += 1

    return max(markdown_tables, html_tables)


def _compute_link_density(text: str) -> float:
    """Compute the ratio of characters inside links to total text length.

    Counts both link text and link URLs as link characters.
    Returns a value in [0.0, 1.0].
    """
    if not text.strip():
        return 0.0
    # Markdown links: [text](url) — count text + URL
    markdown_links = re.findall(r"\[([^]]*)\]\(([^)]*)\)", text)
    # HTML links: <a href="...">text</a>
    html_links = re.findall(
        r"<a[^>]*href=[\"\']([^\"\']*)[\"\'][^>]*>([^<]*)</a>",
        text,
        re.IGNORECASE,
    )

    total_link_chars = 0
    for link_text, url in markdown_links:
        total_link_chars += len(link_text) + len(url)
    for url, link_text in html_links:
        total_link_chars += len(url) + len(link_text)

    total = len(text)
    if total == 0:
        return 0.0
    return total_link_chars / total


def _compute_boilerplate_ratio(text: str) -> float:
    """Estimate the ratio of boilerplate/navigational text.

    Checks for common boilerplate patterns: navigation lines,
    cookie consent, social sharing, copyright notices.
    """
    if not text.strip():
        return 0.0

    boilerplate_patterns = [
        r"privacy\s*policy",
        r"terms\s*of\s*use",
        r"cookie[s]?\s*consent",
        r"all\s*rights\s*reserved",
        r"follow us",
        r"share this",
        r"back to top",
        r"powered by",
        r"blog archive",
        r"previous post",
        r"next post",
        r"comments? closed",
        r"return to",
        r"sitemap",
        r"advertise with",
        r"contact us",
        r"about us",
        r"disclaimer",
    ]
    combined = " | ".join(boilerplate_patterns)
    boilerplate_matches = len(re.findall(combined, text, re.IGNORECASE))

    # Count lines that are purely navigational
    nav_pattern = r"^\s*(?:[-*+]\s+)?(?:[a-zA-Z][a-zA-Z\s&,-]+)\s*$"
    nav_lines = len(re.findall(nav_pattern, text, re.MULTILINE))
    total_lines = max(len(text.split("\n")), 1)

    # Score: combination of pattern matches and nav-line ratio
    pattern_score = min(boilerplate_matches / max(total_lines, 1), 1.0)
    nav_score = min(nav_lines / max(total_lines, 1), 1.0)
    return pattern_score * 0.6 + nav_score * 0.4


def _check_title_present(text: str, declared_title: str | None) -> bool:
    """Check whether a title is present in the content."""
    if declared_title and declared_title.strip():
        return True
    # Check for H1 or top-level heading
    h1 = re.findall(r"^# (.+)$", text, re.MULTILINE)
    if h1:
        return True
    # Check for HTML h1
    html_h1 = re.findall(r"<h1[^>]*>([^<]+)</h1>", text, re.IGNORECASE)
    if html_h1:
        return True
    return False


def _check_content_type_consistency(
    mime_type: str | None,
    expected_mime_type: str | None,
    text: str,
    raw_content: bytes,
) -> bool:
    """Check whether the MIME type is consistent with the content."""
    if not mime_type:
        return True  # No MIME info — assume consistent
    declared = mime_type.lower().strip()
    expected = (expected_mime_type or "").lower().strip()

    # Decode raw content for HTML detection
    try:
        raw_text = raw_content.decode("utf-8", errors="replace")
    except Exception:
        raw_text = raw_content.decode("latin-1", errors="replace")

    # If declared is HTML but content is clearly plain text
    if "text/html" in declared and _is_plain_text_heuristic(text):
        if expected and "text/html" not in expected:
            return False
    # If declared is plain text but content has HTML structure
    if "text/plain" in declared:
        # Check for HTML-like patterns in the raw content
        html_patterns = [
            r"<html",
            r"<body",
            r"<div[\s>]",
            r"<span[\s>]",
            r"<head[\s>]",
            r"<meta[\s>]",
        ]
        for pat in html_patterns:
            if re.search(pat, raw_text, re.IGNORECASE):
                return False
    return True


def _is_plain_text_heuristic(text: str) -> bool:
    """Heuristic to detect if content is plain text despite HTML MIME."""
    tag_count = len(re.findall(r"<[^>]+>", text))
    if tag_count > 0:
        return False
    # Check for HTML-like patterns
    html_patterns = [
        r"<div",
        r"<span",
        r"<p>",
        r"</?body",
    ]
    for pat in html_patterns:
        if re.search(pat, text, re.IGNORECASE):
            return False
    return True


def _count_anti_bot_markers(text: str) -> int:
    """Count anti-bot challenge markers.

    Returns the number of distinct anti-bot patterns found.
    """
    markers = [
        r"cloudflare",
        r"please verify you are human",
        r"captcha",
        r"bot\s*protection",
        r"browser\s*check",
        r"verify\s*captcha",
        r"access\s*denied",
        r"your\s*browser\s*was? blocked",
        r"cf-challenge",
        r"js_challenge",
        r"permanently blocked",
        r"security\s*check",
        r"anti[- ]bot",
        r"robot\s*detection",
        r"disable\s*your\s*ad\s*blocker",
        r"supported\s*browser",
    ]
    count = 0
    lower_text = text.lower()
    for marker in markers:
        if re.search(marker, lower_text, re.IGNORECASE):
            count += 1
    return count


def _compute_language_confidence(text: str) -> float:
    """Estimate language confidence from text characteristics.

    Returns a value in [0.0, 1.0].
    """
    if not text.strip():
        return 0.0

    # Check for non-ASCII characters
    non_ascii = sum(1 for c in text if ord(c) > 127)
    total = len(text)
    if total == 0:
        return 0.0

    non_ascii_ratio = non_ascii / total

    # High non-ASCII ratio with coherent structure suggests valid
    # non-English text. Very high ratio with no structure suggests
    # encoding issues.
    sentences = len(re.split(r"[.!?]+", text))
    words = len(text.split())
    avg_sentence_length = words / max(sentences, 1)

    # Confidence is higher when there's reasonable sentence structure
    if avg_sentence_length > 5 and avg_sentence_length < 50:
        base_confidence = 0.8
    elif words > 10:
        base_confidence = 0.5
    else:
        base_confidence = 0.2

    # Penalize if non-ASCII is very high (possible encoding issue)
    if non_ascii_ratio > 0.5:
        base_confidence *= 0.7

    return min(base_confidence, 1.0)


def _compute_query_term_coverage(text: str, query_terms: Iterable[str] | None) -> float:
    """Compute coverage of expected query terms in the content.

    Returns a value in [0.0, 1.0].
    """
    if not query_terms:
        return 0.0  # No query terms to check

    terms = [t.strip().lower() for t in query_terms if t.strip()]
    if not terms:
        return 0.0

    lower_text = text.lower()
    covered = sum(1 for term in terms if term in lower_text)
    return covered / len(terms)


def _compute_extraction_confidence(
    visible_length: int,
    heading_count: int,
    paragraph_count: int,
) -> float:
    """Compute extraction method confidence from structural signals.

    Returns a value in [0.0, 1.0].
    """
    if visible_length == 0:
        return 0.0

    # Confidence factors
    length_score = min(visible_length / 500.0, 1.0)
    structure_score = min((heading_count + paragraph_count) / 5.0, 1.0)

    # Blend: length and structure both matter
    return round(0.5 * length_score + 0.5 * structure_score, 4)


def _compute_code_to_prose_ratio(text: str) -> float:
    """Estimate the ratio of code blocks to prose.

    Returns a value in [0.0, 1.0].
    """
    code_blocks = len(re.findall(r"^```", text, re.MULTILINE))
    html_pre = len(re.findall(r"<pre[\s>]", text, re.IGNORECASE))
    code_regions = max(code_blocks // 2, html_pre)

    lines = text.split("\n")
    total_lines = max(len(lines), 1)
    code_lines = sum(1 for line in lines if line.strip().startswith("```"))
    if code_regions > 0:
        code_lines = max(code_lines, code_regions * 3)

    return code_lines / total_lines


def _count_required_structured(
    heading_count: int,
    table_count: int,
    list_count: int,
) -> int:
    """Count present structured fields.

    Returns the number of structured element types present.
    """
    present = 0
    if heading_count > 0:
        present += 1
    if table_count > 0:
        present += 1
    if list_count > 0:
        present += 1
    return present
