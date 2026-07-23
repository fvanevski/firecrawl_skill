"""Configurable quality-evaluation thresholds.

All thresholds are versioned under ``quality-v1``.  Callers may override
individual values via environment variables or by instantiating a custom
``QualityConfig`` and passing it to the evaluator or service.

## Threshold design principles

* No single metric independently determines disposition.
* Anti-bot markers are a hard-fail regardless of other signals.
* Short valid content is acceptable.
* Long anti-bot content is rejected.
* Ambiguous cases are identifiable for semantic adjudication.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QualityConfig:
    """Versioned quality-evaluation thresholds.

    Args:
        quality_version: Version string for this threshold set.
        min_visible_text_length: Minimum visible text length for
            ``acceptable`` (short valid content is still acceptable
            if structural and anti-bot signals are clean).
        max_link_density: Maximum link density for ``acceptable``.
        max_boilerplate_ratio: Maximum boilerplate ratio for
            ``acceptable``.
        min_heading_count: Minimum heading count for ``acceptable``
            (content with zero headings is not automatically rejected).
        min_paragraph_count: Minimum paragraph count for ``acceptable``.
        min_title_present: Whether a title is required for
            ``acceptable`` (short content may be acceptable without).
        anti_bot_hard_fail: When ``True``, any anti-bot markers cause
            a hard fail (``poor``).
        max_duplicate_content_similarity: Maximum duplicate similarity
            for ``acceptable``.
        min_query_term_coverage: Minimum query-term coverage for
            ``acceptable``.
        min_language_confidence: Minimum language confidence for
            ``acceptable``.
        min_extraction_method_confidence: Minimum extraction method
            confidence for ``acceptable``.
        max_parser_warnings: Maximum parser warnings before
            degradation.
        min_table_count: Minimum table count for ``acceptable``
            (not required for prose-only content).
        min_list_count: Minimum list count for ``acceptable``
            (not required for prose-only content).
    """

    quality_version: str = "quality-v1"

    # Length thresholds
    min_visible_text_length: int = 30

    # Structural thresholds
    min_heading_count: int = 0
    min_paragraph_count: int = 1
    min_table_count: int = 0
    min_list_count: int = 0

    # Density thresholds (0.0 – 1.0)
    max_link_density: float = 0.3
    max_boilerplate_ratio: float = 0.4
    max_duplicate_content_similarity: float = 0.6
    min_query_term_coverage: float = 0.0
    min_language_confidence: float = 0.3
    min_extraction_method_confidence: float = 0.3

    # Boolean thresholds
    min_title_present: bool = False
    anti_bot_hard_fail: bool = True

    # Warning thresholds
    max_parser_warnings: int = 3

    # Content-type consistency
    # When False and content_type_consistent is also False → degradation.

    @classmethod
    def from_env(cls) -> "QualityConfig":
        """Build a config from environment variables.

        All values are optional — omitted env vars use the defaults
        defined in the dataclass.
        """
        import os

        def _int(name: str, default: int) -> int:
            val = os.environ.get(name)
            if val is not None:
                return int(val)
            return default

        def _float(name: str, default: float) -> float:
            val = os.environ.get(name)
            if val is not None:
                return float(val)
            return default

        def _bool(name: str, default: bool) -> bool:
            val = os.environ.get(name)
            if val is None:
                return default
            return val.lower() in ("1", "true", "yes")

        return cls(
            quality_version=os.environ.get("QUALITY_VERSION", "quality-v1"),
            min_visible_text_length=_int("QUALITY_MIN_VISIBLE_TEXT_LENGTH", 30),
            min_heading_count=_int("QUALITY_MIN_HEADING_COUNT", 0),
            min_paragraph_count=_int("QUALITY_MIN_PARAGRAPH_COUNT", 1),
            min_table_count=_int("QUALITY_MIN_TABLE_COUNT", 0),
            min_list_count=_int("QUALITY_MIN_LIST_COUNT", 0),
            max_link_density=_float("QUALITY_MAX_LINK_DENSITY", 0.3),
            max_boilerplate_ratio=_float("QUALITY_MAX_BOILERPLATE_RATIO", 0.4),
            max_duplicate_content_similarity=_float(
                "QUALITY_MAX_DUPLICATE_CONTENT_SIMILARITY", 0.6
            ),
            min_query_term_coverage=_float("QUALITY_MIN_QUERY_TERM_COVERAGE", 0.0),
            min_language_confidence=_float("QUALITY_MIN_LANGUAGE_CONFIDENCE", 0.3),
            min_extraction_method_confidence=_float(
                "QUALITY_MIN_EXTRACTION_METHOD_CONFIDENCE", 0.3
            ),
            min_title_present=_bool("QUALITY_MIN_TITLE_PRESENT", False),
            anti_bot_hard_fail=_bool("QUALITY_ANTI_BOT_HARD_FAIL", True),
            max_parser_warnings=_int("QUALITY_MAX_PARSER_WARNINGS", 3),
        )
