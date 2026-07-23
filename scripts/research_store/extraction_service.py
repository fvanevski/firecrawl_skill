"""ExtractionService — plan and execute extraction attempts (issue #40).

This service owns the extraction-attempt lifecycle:
- Creating ordered attempts per candidate.
- Recording raw and normalized blob references.
- Evaluating quality and assigning disposition.
- Selecting the final successful attempt.
- Preserving failed and superseded attempts as audit history.

The service never mutates or deletes prior attempts.  Retries append
new rows with incremented ``attempt_number`` and a ``retry_parent_id``
link.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable
from uuid import UUID

from .config import StoreConfig
from .domain import (
    BlobReference,
    ExtractionAttempt,
    ExtractionQualityMetrics,
    utcnow,
)


class ExtractionError(Exception):
    """Base exception for extraction failures."""


class ExtractionAttemptError(ExtractionError):
    """Raised when an extraction attempt fails."""

    def __init__(
        self,
        message: str,
        failure_class: str = "internal",
        http_status: int | None = None,
    ):
        super().__init__(message)
        self.failure_class = failure_class
        self.http_status = http_status


class ExtractionService:
    """Coordinate extraction-attempt creation, quality evaluation, and selection.

    Args:
        uow_factory: Callable that returns a ``UnitOfWork`` context manager.
        blob_store: Optional blob store for raw/normalized payload writes.

    The service uses the ``ExtractionAttemptRepository`` port through the
    unit-of-work to ensure transactional integrity between attempt
    creation, blob writes, and corpus-ingestion linkage.
    """

    def __init__(
        self,
        uow_factory: Callable[[], Any],
        blob_store=None,
        config: StoreConfig | None = None,
    ):
        self.uow_factory = uow_factory
        self.blob_store = blob_store
        self.config = config or StoreConfig.from_env()

    def create_attempt(
        self,
        candidate_id: UUID,
        run_id: UUID,
        invocation_id: UUID | None = None,
        method: str = "firecrawl_main_content",
        method_version: str | None = None,
        requested_format: str | None = None,
        retry_parent_id: UUID | None = None,
        start_time: datetime | None = None,
    ) -> UUID:
        """Create a new extraction attempt row and return its ID.

        The attempt is recorded with ``exit_status='succeeded'`` and
        disposition ``unassessed`` as a placeholder.  The caller must
        update the attempt with actual results via ``complete_attempt``.

        Args:
            candidate_id: The candidate this attempt belongs to.
            run_id: The research run this attempt is part of.
            invocation_id: Optional invocation event ID.
            method: Extraction method name.
            method_version: Implementation version string.
            requested_format: Target output format.
            retry_parent_id: Parent attempt ID for retry lineage.
            start_time: When extraction began.

        Returns:
            The UUID of the newly created attempt.
        """
        now = start_time or utcnow()
        max_retries = 3
        for retry in range(max_retries):
            with self.uow_factory() as uow:
                attempts = uow.extraction_attempts.list_attempts_for_candidate(
                    candidate_id, run_id=run_id
                )
                next_number = len(attempts) + 1
                try:
                    attempt_id = uow.extraction_attempts.create_attempt(
                        candidate_id=candidate_id,
                        run_id=run_id,
                        invocation_id=invocation_id,
                        attempt_number=next_number,
                        method=method,
                        method_version=method_version or self.config.parser_version,
                        requested_format=requested_format,
                        start_time=now,
                        end_time=None,
                        exit_status="succeeded",
                        http_status=None,
                        backend_status=None,
                        raw_blob=None,
                        normalized_blob=None,
                        parser_used=None,
                        quality_metrics=None,
                        failure_class="none",
                        retry_parent_id=retry_parent_id,
                        disposition="unassessed",
                        error_message=None,
                        selection_reason=None,
                    )
                    uow.commit()
                    return attempt_id
                except Exception as exc:
                    # UNIQUE (candidate_id, attempt_number) collision due to
                    # concurrent callers computing the same next_number.
                    # Retry with an incremented attempt_number after refreshing
                    # the candidate's attempt list.
                    if retry < max_retries - 1 and self._is_unique_violation(exc):
                        continue
                    raise
        # Fallback: should never reach here, but mypy needs a return.
        raise ExtractionAttemptError(
            f"failed to create attempt after {max_retries} retries",
            failure_class="internal",
        )

    @staticmethod
    def _is_unique_violation(exc: Exception) -> bool:
        """Return True if *exc* is a database unique-constraint violation.

        Detects PostgreSQL ``psycopg.errors.UniqueViolation`` and the generic
        ``sqlalchemy.exc.IntegrityError`` / ``sqlite3.IntegrityError`` so that
        callers can retry with an incremented ``attempt_number``.
        """
        # psycopg 3 (native)
        try:
            import psycopg.errors

            if isinstance(exc, psycopg.errors.UniqueViolation):
                return True
        except ImportError:
            pass
        # psycopg 3 with SQLAlchemy wrapper
        try:
            from sqlalchemy.exc import IntegrityError

            if isinstance(exc, IntegrityError):
                return True
        except ImportError:
            pass
        # sqlite3 / built-in
        import sqlite3

        if isinstance(exc, sqlite3.IntegrityError):
            return True
        return False

    def complete_attempt(
        self,
        attempt_id: UUID,
        exit_status: str,
        raw_blob: BlobReference | None = None,
        normalized_blob: BlobReference | None = None,
        parser_used: str | None = None,
        quality_metrics: ExtractionQualityMetrics | None = None,
        failure_class: str = "none",
        http_status: int | None = None,
        backend_status: str | None = None,
        end_time: datetime | None = None,
        error_message: str | None = None,
    ) -> ExtractionAttempt:
        """Record the actual results of an extraction attempt.

        This is the primary method for committing extraction outcomes.
        It updates the attempt row with timing, blobs, quality metrics,
        and failure classification.

        **Partial-update semantics (COALESCE):**  When a blob argument
        (``raw_blob``, ``normalized_blob``) or ``parser_used`` is ``None``,
        the corresponding database column is *not* changed — the existing
        value is preserved.  This allows callers to update only the fields
        they have values for.  To explicitly clear a blob reference, call
        ``evaluate_and_set_disposition`` or ``record_quality_metrics``
        through the repository port with ``None``.

        Args:
            attempt_id: The attempt to complete.
            exit_status: One of succeeded, partial, failed, cancelled.
            raw_blob: Content-addressed reference to the raw payload.
                      Pass ``None`` to leave the existing value unchanged.
            normalized_blob: Content-addressed reference to the normalized
                             artifact.  Pass ``None`` to leave unchanged.
            parser_used: Parser version used for extraction.  Pass ``None``
                         to leave unchanged.
            quality_metrics: Deterministic quality evaluation.  Pass ``None``
                             to leave unchanged.  Note that
                             ``evaluate_and_set_disposition`` will
                             overwrite this field if called afterward.
            failure_class: Classification of failure (if failed).
            http_status: HTTP status code from the backend.
            backend_status: Backend-specific status string.
            end_time: When extraction ended.
            error_message: Human-readable error description.

        Returns:
            The completed ``ExtractionAttempt`` domain model.

        Raises:
            ExtractionAttemptError: If the attempt cannot be found.
        """
        now = end_time or utcnow()
        with self.uow_factory() as uow:
            existing = uow.extraction_attempts.get_attempt(attempt_id)
            if existing is None:
                raise ExtractionAttemptError(
                    f"attempt {attempt_id} not found",
                    failure_class="internal",
                )
            uow.extraction_attempts.complete_attempt(
                attempt_id=attempt_id,
                exit_status=exit_status,
                raw_blob=raw_blob,
                normalized_blob=normalized_blob,
                parser_used=parser_used,
                quality_metrics=quality_metrics,
                failure_class=failure_class,
                http_status=http_status,
                backend_status=backend_status,
                end_time=now,
                error_message=error_message,
            )
            uow.commit()
            return uow.extraction_attempts.get_attempt(attempt_id)

    def evaluate_and_set_disposition(
        self,
        attempt_id: UUID,
        quality_metrics: ExtractionQualityMetrics,
        disposition: str,
    ) -> ExtractionAttempt:
        """Evaluate quality metrics and set the attempt disposition.

        Quality metrics and disposition are stored separately from the
        attempt record so that re-evaluation does not mutate the attempt
        itself.

        **Authoritative quality source:**  This method performs a full
        overwrite of the ``quality_metrics`` JSONB column.  If
        ``complete_attempt`` was previously called with quality_metrics,
        those values will be replaced by the metrics passed here.  Callers
        that wish to update quality metrics without changing disposition
        should use ``evaluate_and_set_disposition`` with the existing
        disposition, or update metrics directly through the repository
        port.

        Args:
            attempt_id: The attempt to evaluate.
            quality_metrics: Deterministic quality evaluation results.
                             Replaces any prior quality_metrics set by
                             ``complete_attempt``.
            disposition: One of acceptable, poor, ambiguous, unassessed.

        Returns:
            The attempt with updated quality metrics.
        """
        with self.uow_factory() as uow:
            existing = uow.extraction_attempts.get_attempt(attempt_id)
            if existing is None:
                raise ExtractionAttemptError(
                    f"attempt {attempt_id} not found",
                    failure_class="internal",
                )
            uow.extraction_attempts.record_quality_metrics(attempt_id, quality_metrics)
            uow.extraction_attempts.update_disposition(attempt_id, disposition)
            uow.commit()
            return uow.extraction_attempts.get_attempt(attempt_id)

    def select_final_attempt(
        self,
        candidate_id: UUID,
        attempt_id: UUID,
        selection_reason: str,
    ) -> None:
        """Mark an attempt as the selected final attempt for a candidate.

        This does NOT delete or overwrite prior attempts.  The selected
        attempt gets a ``selection_reason`` and the previous selected
        attempt (if any) is unselected.

        Corpus ingestion must reference this attempt's normalized blob
        (or the attempt ID itself) to establish provenance.

        Args:
            candidate_id: The candidate to select for.
            attempt_id: The attempt to select.
            selection_reason: Why this attempt was chosen.
        """
        with self.uow_factory() as uow:
            existing = uow.extraction_attempts.get_attempt(attempt_id)
            if existing is None:
                raise ExtractionAttemptError(
                    f"attempt {attempt_id} not found",
                    failure_class="internal",
                )
            uow.extraction_attempts.select_final_attempt(
                candidate_id, attempt_id, selection_reason
            )
            uow.commit()

    def get_selected_attempt(self, candidate_id: UUID) -> ExtractionAttempt | None:
        """Return the currently selected final attempt for a candidate.

        Args:
            candidate_id: The candidate to query.

        Returns:
            The selected ``ExtractionAttempt`` or ``None``.
        """
        with self.uow_factory() as uow:
            result = uow.extraction_attempts.get_selected_attempt(candidate_id)
            if result is None:
                return None
            return ExtractionAttempt.from_mapping(result)

    def list_attempts(
        self,
        candidate_id: UUID,
        run_id: UUID | None = None,
    ) -> list[ExtractionAttempt]:
        """List all attempts for a candidate, ordered by attempt_number.

        Args:
            candidate_id: The candidate to query.
            run_id: Optional run filter.

        Returns:
            Ordered list of ``ExtractionAttempt`` domain models.
        """
        with self.uow_factory() as uow:
            rows = uow.extraction_attempts.list_attempts_for_candidate(
                candidate_id, run_id=run_id
            )
            return [ExtractionAttempt.from_mapping(r) for r in rows]

    def store_raw_blob(self, content: bytes) -> BlobReference:
        """Write raw extraction content to blob store and return reference.

        Args:
            content: Raw extraction output bytes.

        Returns:
            Content-addressed ``BlobReference``.

        Raises:
            ExtractionError: If blob store is not configured.
        """
        if self.blob_store is None:
            raise ExtractionError(
                "blob_store is required for raw payload persistence; "
                "configure BLOB_ROOT or pass blob_store to ExtractionService"
            )
        from io import BytesIO

        return self.blob_store.put(BytesIO(content), None)

    def store_normalized_blob(self, content: bytes) -> BlobReference:
        """Write normalized extraction content to blob store and return reference.

        Args:
            content: Normalized extraction output bytes.

        Returns:
            Content-addressed ``BlobReference``.

        Raises:
            ExtractionError: If blob store is not configured.
        """
        if self.blob_store is None:
            raise ExtractionError(
                "blob_store is required for normalized payload persistence; "
                "configure BLOB_ROOT or pass blob_store to ExtractionService"
            )
        from io import BytesIO

        return self.blob_store.put(BytesIO(content), None)

    def create_retry(
        self,
        candidate_id: UUID,
        run_id: UUID,
        parent_attempt_id: UUID,
        method: str = "firecrawl_full_page",
        method_version: str | None = None,
        invocation_id: UUID | None = None,
    ) -> UUID:
        """Create a retry attempt linked to a failed parent attempt.

        The retry gets an incremented ``attempt_number`` and a
        ``retry_parent_id`` that preserves the full retry lineage.

        Args:
            candidate_id: The candidate to retry for.
            run_id: The research run.
            parent_attempt_id: The failed attempt to retry.
            method: New extraction method to try.
            method_version: Implementation version.
            invocation_id: Optional invocation event ID.

        Returns:
            The new attempt's UUID.
        """
        max_retries = 3
        for retry in range(max_retries):
            with self.uow_factory() as uow:
                attempts = uow.extraction_attempts.list_attempts_for_candidate(
                    candidate_id, run_id=run_id
                )
                next_number = len(attempts) + 1
                try:
                    attempt_id = uow.extraction_attempts.create_attempt(
                        candidate_id=candidate_id,
                        run_id=run_id,
                        invocation_id=invocation_id,
                        attempt_number=next_number,
                        method=method,
                        method_version=method_version or self.config.parser_version,
                        requested_format=None,
                        start_time=utcnow(),
                        end_time=None,
                        exit_status="succeeded",
                        http_status=None,
                        backend_status=None,
                        raw_blob=None,
                        normalized_blob=None,
                        parser_used=None,
                        quality_metrics=None,
                        failure_class="none",
                        retry_parent_id=parent_attempt_id,
                        disposition="unassessed",
                        error_message=None,
                        selection_reason=None,
                    )
                    uow.commit()
                    return attempt_id
                except Exception as exc:
                    if retry < max_retries - 1 and self._is_unique_violation(exc):
                        continue
                    raise
        raise ExtractionAttemptError(
            f"failed to create retry after {max_retries} retries",
            failure_class="internal",
        )
