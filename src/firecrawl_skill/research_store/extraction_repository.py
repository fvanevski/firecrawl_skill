"""Extraction-attempt repository protocol (issue #40).

Defines the ``ExtractionAttemptRepository`` port that the
``ExtractionService`` uses to persist and query extraction attempts.

The protocol is defined in ``ports.py``; this module re-exports it for
convenience and provides the ``ExtractionAttemptRepository`` name so
that ``ExtractionService`` can import from a single, focused module.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from .domain import BlobReference, ExtractionQualityMetrics


class ExtractionAttemptRepository(Protocol):
    """Persist and query extraction-attempt records.

    All operations are transactional — they must be executed within a
    ``UnitOfWork`` context so that attempt creation, blob references,
    and corpus-ingestion linkage remain atomic.
    """

    def create_attempt(
        self,
        candidate_id: UUID,
        run_id: UUID,
        invocation_id: UUID | None,
        attempt_number: int,
        method: str,
        method_version: str,
        requested_format: str | None,
        start_time: Any,
        end_time: Any,
        exit_status: str,
        http_status: int | None,
        backend_status: str | None,
        raw_blob: BlobReference | None,
        normalized_blob: BlobReference | None,
        parser_used: str | None,
        quality_metrics: ExtractionQualityMetrics | None,
        failure_class: str,
        retry_parent_id: UUID | None,
        disposition: str,
        error_message: str | None,
        selection_reason: str | None,
    ) -> UUID: ...

    def complete_attempt(
        self,
        attempt_id: UUID,
        exit_status: str,
        raw_blob: BlobReference | None,
        normalized_blob: BlobReference | None,
        parser_used: str | None,
        quality_metrics: ExtractionQualityMetrics | None,
        failure_class: str,
        http_status: int | None,
        backend_status: str | None,
        end_time: Any,
        error_message: str | None,
    ) -> None: ...

    def update_disposition(self, attempt_id: UUID, disposition: str) -> None: ...

    def select_final_attempt(
        self,
        candidate_id: UUID,
        attempt_id: UUID,
        selection_reason: str,
    ) -> None: ...

    def get_attempt(
        self, attempt_id: UUID, run_id: UUID | None = None
    ) -> dict[str, Any]: ...

    def list_attempts_for_candidate(
        self,
        candidate_id: UUID,
        *,
        run_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...

    def list_attempts_for_run(
        self,
        run_id: UUID,
        *,
        candidate_id: UUID | None = None,
        method: str | None = None,
        exit_status: str | None = None,
        disposition: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...

    def get_selected_attempt(self, candidate_id: UUID) -> dict[str, Any] | None: ...

    def record_quality_metrics(
        self,
        attempt_id: UUID,
        quality_metrics: ExtractionQualityMetrics,
    ) -> None: ...
