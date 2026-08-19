"""Quality evaluation service.

Bridges the deterministic ``QualityEvaluator`` with the ``ExtractionService``
so extraction attempts are automatically evaluated and assigned a disposition.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ..domain import ExtractionAttempt, ExtractionQualityMetrics
from ..extraction_service import ExtractionAttemptError, ExtractionService
from ..quality_config import QualityConfig
from ..quality_evaluator import evaluate_quality


class QualityEvaluationError(Exception):
    """Raised when quality evaluation fails."""


class QualityService:
    """Auto-evaluate extraction attempts and map metrics to disposition."""

    def __init__(
        self,
        extraction_service: ExtractionService,
        config: QualityConfig | None = None,
    ):
        self.extraction_service = extraction_service
        self.config = config or QualityConfig.from_env()

    def auto_evaluate(self, attempt_id: UUID) -> ExtractionAttempt:
        attempt = self.extraction_service.get_attempt(attempt_id)
        if attempt is None:
            raise ExtractionAttemptError(
                f"attempt {attempt_id} not found",
                failure_class="internal",
            )
        if attempt.raw_blob is None:
            raise QualityEvaluationError(
                f"attempt {attempt_id} has no raw blob for evaluation"
            )
        content = self._read_blob(attempt.raw_blob)
        if content is None:
            raise QualityEvaluationError(
                f"failed to read raw blob {attempt.raw_blob.uri}"
            )
        metrics = evaluate_quality(
            content,
            mime_type=attempt.raw_blob.mime_type,
            config=self.config,
        )
        disposition = self.map_disposition(metrics)
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
        disposition = self.map_disposition(quality_metrics)
        return self.extraction_service.evaluate_and_set_disposition(
            attempt_id=attempt_id,
            quality_metrics=quality_metrics,
            disposition=disposition,
        )

    def map_disposition(self, metrics: ExtractionQualityMetrics) -> str:
        if self.config.anti_bot_hard_fail and metrics.anti_bot_markers > 0:
            return "poor"
        if metrics.visible_text_length == 0:
            return "poor"
        if metrics.boilerplate_ratio > self.config.max_boilerplate_ratio:
            return "poor"
        if metrics.link_density > self.config.max_link_density:
            return "poor"
        if (
            metrics.duplicate_content_similarity
            > self.config.max_duplicate_content_similarity
        ):
            return "poor"
        if not metrics.content_type_consistent:
            return "poor"

        has_structure = (
            metrics.heading_count >= self.config.min_heading_count
            and metrics.paragraph_count >= self.config.min_paragraph_count
        )
        has_content = metrics.visible_text_length >= self.config.min_visible_text_length

        if not has_content and not has_structure:
            if (
                metrics.title_present
                and metrics.extraction_method_confidence
                >= self.config.min_extraction_method_confidence
            ):
                return "acceptable"
            return "ambiguous"

        if has_content and not has_structure:
            if (
                metrics.title_present
                and metrics.boilerplate_ratio < self.config.max_boilerplate_ratio * 0.5
            ):
                return "acceptable"
            return "ambiguous"

        if has_content and has_structure:
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
            if (
                metrics.extraction_method_confidence
                >= self.config.min_extraction_method_confidence
            ):
                return "acceptable"
            return "ambiguous"

        return "ambiguous"

    def _read_blob(self, blob_ref: Any) -> bytes | None:
        if self.extraction_service.blob_store is None:
            return None
        try:
            return self.extraction_service.blob_store.get(blob_ref.uri).read()
        except Exception:  # noqa: BLE001
            return None

    def evaluate_from_content(
        self,
        attempt_id: UUID,
        content: bytes,
        mime_type: str | None = None,
        title: str | None = None,
        query_terms: list[str] | None = None,
    ) -> ExtractionAttempt:
        metrics = evaluate_quality(
            content,
            mime_type=mime_type,
            title=title,
            query_terms=query_terms,
            config=self.config,
        )
        return self.evaluate_with_metrics(attempt_id, metrics)


__all__ = ["QualityEvaluationError", "QualityService"]
