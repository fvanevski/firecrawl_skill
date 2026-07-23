"""Quality evaluation service.

Bridges the deterministic ``QualityEvaluator`` with the
``ExtractionService`` so that extraction attempts are automatically
evaluated and assigned a disposition.

## Disposition mapping

The service maps quality metrics to one of:

* **acceptable** — content passes all structural and signal checks.
* **poor** — content fails one or more hard checks (anti-bot,
  excessive boilerplate, etc.).
* **ambiguous** — content has mixed signals that warrant semantic
  adjudication.
* **unassessed** — default state before evaluation.

## Invariants

* No single metric independently determines disposition.
* Anti-bot markers are a hard-fail regardless of length.
* Short valid content may be acceptable.
* The semantic adjudication result remains a versioned proposal and
  never overwrites deterministic extraction history.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .domain import (
    ExtractionAttempt,
    ExtractionQualityMetrics,
)
from .extraction_service import (
    ExtractionAttemptError,
    ExtractionService,
)
from .quality_config import QualityConfig
from .quality_evaluator import evaluate_quality


class QualityEvaluationError(Exception):
    """Raised when quality evaluation fails."""


class QualityService:
    """Auto-evaluate extraction attempts and map metrics to disposition.

    Args:
        extraction_service: The ``ExtractionService`` to evaluate.
        config: Quality thresholds.  Defaults to
            ``QualityConfig.from_env()``.

    Usage::

        service = ExtractionService(...)
        quality = QualityService(service)

        # After completing an attempt:
        quality.auto_evaluate(attempt_id)

        # Or with manual metrics:
        quality.evaluate_with_metrics(
            attempt_id, quality_metrics
        )
    """

    def __init__(
        self,
        extraction_service: ExtractionService,
        config: QualityConfig | None = None,
    ):
        self.extraction_service = extraction_service
        self.config = config or QualityConfig.from_env()

    def auto_evaluate(self, attempt_id: UUID) -> ExtractionAttempt:
        """Auto-evaluate an attempt using its stored content and metrics.

        Reads the attempt's raw blob, computes quality metrics from the
        blob content, maps to a disposition, and updates the attempt.

        Args:
            attempt_id: The attempt to evaluate.

        Returns:
            The updated ``ExtractionAttempt``.

        Raises:
            QualityEvaluationError: If evaluation fails.
            ExtractionAttemptError: If the attempt is not found.
        """
        attempt = self.extraction_service.get_selected_attempt(
            UUID("00000000-0000-0000-0000-000000000000")
        )
        # Fall back to listing attempts to find by ID
        attempts = self.extraction_service.list_attempts(attempt_id, run_id=None)
        for a in attempts:
            if a.id == attempt_id:
                attempt = a
                break
        else:
            raise ExtractionAttemptError(
                f"attempt {attempt_id} not found",
                failure_class="internal",
            )

        if attempt.raw_blob is None:
            raise QualityEvaluationError(
                f"attempt {attempt_id} has no raw blob for evaluation"
            )

        # Read raw blob content
        content = self._read_blob(attempt.raw_blob)
        if content is None:
            raise QualityEvaluationError(
                f"failed to read raw blob {attempt.raw_blob.uri}"
            )

        # Compute quality metrics from content
        metrics = evaluate_quality(
            content,
            mime_type=attempt.raw_blob.mime_type,
            config=self.config,
        )

        # Map metrics to disposition
        disposition = self.map_disposition(metrics)

        # Update the attempt
        return self.extraction_service.evaluate_and_set_disposition(
            attempt_id=attempt_id,
            quality_metrics=metrics,
            disposition=disposition,
        )

    def evaluate_with_metrics(
        self,
        attempt_id: UUID,
        quality_metrics: ExtractionQualityMetrics,
    ) -> ExtractionAttempt:
        """Evaluate an attempt using pre-computed quality metrics.

        Maps the metrics to a disposition and updates the attempt.

        Args:
            attempt_id: The attempt to evaluate.
            quality_metrics: Pre-computed quality metrics.

        Returns:
            The updated ``ExtractionAttempt``.
        """
        disposition = self.map_disposition(quality_metrics)
        return self.extraction_service.evaluate_and_set_disposition(
            attempt_id=attempt_id,
            quality_metrics=quality_metrics,
            disposition=disposition,
        )

    def map_disposition(self, metrics: ExtractionQualityMetrics) -> str:
        """Map quality metrics to a disposition.

        This is the core disposition-mapping logic.  It implements the
        invariant that no single metric independently determines
        disposition.

        Args:
            metrics: Quality metrics to evaluate.

        Returns:
            One of ``acceptable``, ``poor``, ``ambiguous``,
            ``unassessed``.
        """
        # Hard-fail: anti-bot markers
        if self.config.anti_bot_hard_fail and metrics.anti_bot_markers > 0:
            return "poor"

        # Hard-fail: no visible text
        if metrics.visible_text_length == 0:
            return "poor"

        # Hard-fail: excessive boilerplate
        if metrics.boilerplate_ratio > self.config.max_boilerplate_ratio:
            return "poor"

        # Hard-fail: excessive link density
        if metrics.link_density > self.config.max_link_density:
            return "poor"

        # Hard-fail: high duplicate content
        if (
            metrics.duplicate_content_similarity
            > self.config.max_duplicate_content_similarity
        ):
            return "poor"

        # Hard-fail: content type inconsistency
        if not metrics.content_type_consistent:
            return "poor"

        # Check for ambiguous signals: content exists but structure is weak
        has_structure = (
            metrics.heading_count >= self.config.min_heading_count
            and metrics.paragraph_count >= self.config.min_paragraph_count
        )
        has_content = metrics.visible_text_length >= self.config.min_visible_text_length

        if not has_content and not has_structure:
            # Very little content — could be a valid short page or
            # a failed extraction. Check other signals.
            if (
                metrics.title_present
                and metrics.extraction_method_confidence
                >= self.config.min_extraction_method_confidence
            ):
                return "acceptable"
            return "ambiguous"

        if has_content and not has_structure:
            # Has text but no structure — could be a valid short page
            # or a poorly extracted page.
            if (
                metrics.title_present
                and metrics.boilerplate_ratio < self.config.max_boilerplate_ratio * 0.5
            ):
                return "acceptable"
            return "ambiguous"

        if has_content and has_structure:
            # Good content with structure — check for degradation signals
            degradation_signals = 0
            if metrics.link_density > self.config.max_link_density * 0.7:
                degradation_signals += 1
            if metrics.boilerplate_ratio > self.config.max_boilerplate_ratio * 0.7:
                degradation_signals += 1
            if metrics.parser_warnings > self.config.max_parser_warnings:
                degradation_signals += 1
            if metrics.language_confidence < self.config.min_language_confidence:
                degradation_signals += 1

            if degradation_signals >= 2:
                return "ambiguous"

            # Acceptable: content with structure and few degradation signals
            if (
                metrics.extraction_method_confidence
                >= self.config.min_extraction_method_confidence
            ):
                return "acceptable"

            return "ambiguous"

        return "ambiguous"

    def _read_blob(self, blob_ref: Any) -> bytes | None:
        """Read content from a blob reference.

        Args:
            blob_ref: A ``BlobReference`` or mapping with blob info.

        Returns:
            The raw bytes, or ``None`` if unreadable.
        """
        if self.extraction_service.blob_store is None:
            return None

        try:
            return self.extraction_service.blob_store.get(blob_ref.uri).read()
        except Exception:
            return None

    def evaluate_from_content(
        self,
        attempt_id: UUID,
        content: bytes,
        mime_type: str | None = None,
        title: str | None = None,
        query_terms: list[str] | None = None,
    ) -> ExtractionAttempt:
        """Evaluate an attempt from raw content bytes.

        Convenience method that computes quality metrics from content
        bytes and updates the attempt.

        Args:
            attempt_id: The attempt to evaluate.
            content: Raw content bytes.
            mime_type: Declared MIME type.
            title: Document title.
            query_terms: Expected query terms.

        Returns:
            The updated ``ExtractionAttempt``.
        """
        metrics = evaluate_quality(
            content,
            mime_type=mime_type,
            title=title,
            query_terms=query_terms,
            config=self.config,
        )
        return self.evaluate_with_metrics(attempt_id, metrics)
