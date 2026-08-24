"""Carry explicit temporal provenance into corpus ingestion.

The facade enriches only requests that carry search-candidate provenance
(either the direct-scrape ``direct_scrape.candidate_id`` nesting or the
orchestrator's top-level ``candidate_id``); every other request is
forwarded to the delegate untouched. Enrichment is the single canonical
implementation shared by the single- and batch-entry points, so the
orchestrator's bounded extraction path and direct scrape share it without
a second temporal normalization.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

from .domain import IngestRequest
from .temporal_candidate import (
    extract_document_temporal_signals,
    parse_provider_datetime,
)

_BLOCKING_SIGNAL_STATUSES = {
    "explicit_provider_conflict",
    "explicit_provider_invalid",
    "explicit_conflict",
    "explicit_invalid",
}


class TemporalCorpusService:
    """Narrow CorpusService facade for candidate temporal propagation."""

    def __init__(self, delegate: Any, uow_factory: Any) -> None:
        self.delegate = delegate
        self.uow_factory = uow_factory

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    @staticmethod
    def _candidate_value(metadata: Any) -> str | UUID | None:
        if not isinstance(metadata, dict):
            return None
        direct = metadata.get("direct_scrape")
        value = direct.get("candidate_id") if isinstance(direct, dict) else None
        if not isinstance(value, (str, UUID)):
            value = metadata.get("candidate_id")
        return value if isinstance(value, (str, UUID)) else None

    def _load_candidate(self, candidate_id: UUID) -> dict[str, Any]:
        with self.uow_factory() as uow:
            candidate = uow.candidates.get_candidate(candidate_id)
        return candidate if isinstance(candidate, dict) else {}

    @staticmethod
    def _resolve_authority(
        observations: list[tuple[str, Any, str]],
    ) -> tuple[datetime | None, str, str]:
        """Resolve explicit observations without precedence-based guessing."""

        parsed: list[tuple[str, datetime]] = []
        invalid = False
        conflict = False
        for source, value, status in observations:
            if status in _BLOCKING_SIGNAL_STATUSES:
                if "conflict" in status:
                    conflict = True
                else:
                    invalid = True
                continue
            if value in (None, ""):
                continue
            normalized = parse_provider_datetime(value)
            if normalized is None:
                invalid = True
                continue
            parsed.append((source, normalized))

        if invalid:
            return None, "explicit_invalid", "none"
        distinct = {value.isoformat() for _, value in parsed}
        if conflict or len(distinct) > 1:
            return None, "explicit_conflict", "none"
        if not parsed:
            return None, "unknown", "none"
        sources = {source for source, _ in parsed}
        value = parsed[0][1]
        if sources == {"candidate"}:
            authority = "explicit_provider_only"
        elif sources == {"document"}:
            authority = "explicit_signal_only"
        elif sources == {"request"}:
            authority = "explicit_request_only"
        else:
            authority = "multiple_consistent_explicit"
        return value, "explicit_valid", authority

    def _enrich_request(
        self, request: IngestRequest, candidate_value: Any
    ) -> IngestRequest:
        candidate_id = UUID(str(candidate_value))
        candidate = self._load_candidate(candidate_id)
        signals = candidate.get("date_signals") or {}
        if not isinstance(signals, dict):
            signals = {}
        direct = request.metadata.get("direct_scrape") if request.metadata else None
        transport = direct.get("transport") if isinstance(direct, dict) else None
        transport = transport if isinstance(transport, dict) else {}
        document = extract_document_temporal_signals(
            request.content,
            mime_type=request.mime_type,
            transport_metadata=transport,
        )

        candidate_publication = candidate.get("published_at")
        candidate_publication_status = str(
            signals.get("publication_status")
            or (
                "previous_explicit_provider"
                if candidate_publication is not None
                else "unknown"
            )
        )
        document_publication = document.get("published_at")
        publication, publication_status, publication_authority = (
            self._resolve_authority(
                [
                    ("request", request.published_at, "explicit_request"),
                    ("candidate", candidate_publication, candidate_publication_status),
                    (
                        "document",
                        document_publication,
                        str(document.get("publication_status") or "unknown"),
                    ),
                ]
            )
        )

        candidate_update_raw = signals.get("updated_date")
        candidate_update_status = str(
            signals.get("update_status")
            or (
                "previous_explicit_provider"
                if candidate_update_raw not in (None, "")
                else "unknown"
            )
        )
        document_update = document.get("updated_at")
        update, update_status, update_authority = self._resolve_authority(
            [
                ("request", request.last_modified, "explicit_request"),
                ("candidate", candidate_update_raw, candidate_update_status),
                (
                    "document",
                    document_update,
                    str(document.get("update_status") or "unknown"),
                ),
            ]
        )

        metadata = dict(request.metadata)
        metadata["temporal_provenance"] = {
            "candidate_id": str(candidate_id),
            "published_at": publication.isoformat()
            if publication is not None
            else None,
            "updated_at": update.isoformat() if update is not None else None,
            "retrieved_at": request.retrieved_at.isoformat(),
            "publication_status": publication_status,
            "update_status": update_status,
            "candidate_publication_status": candidate_publication_status,
            "candidate_update_status": candidate_update_status,
            "document_publication_status": document.get("publication_status")
            or "unknown",
            "document_update_status": document.get("update_status") or "unknown",
            "candidate_publication_signals": signals.get("publication_signals", []),
            "candidate_update_signals": signals.get("update_signals", []),
            "document_publication_signals": document.get("publication_signals", []),
            "document_update_signals": document.get("update_signals", []),
            "structured_temporal_segments": document.get(
                "structured_temporal_segments", []
            ),
            "publication_authority": publication_authority,
            "update_authority": update_authority,
            "retrieval_is_publication": False,
        }
        return replace(
            request,
            published_at=publication,
            last_modified=update.isoformat() if update is not None else None,
            metadata=metadata,
        )

    def prepare_ingest(self, request: IngestRequest) -> Any:
        candidate_value = self._candidate_value(request.metadata)
        if not candidate_value:
            return self.delegate.prepare_ingest(request)
        return self.delegate.prepare_ingest(
            self._enrich_request(request, candidate_value)
        )

    def _enrich_batch_item(self, item: Any) -> Any:
        if isinstance(item, IngestRequest):
            request = item
            container: Any = None
        elif isinstance(item, dict):
            request = item.get("request")
            container = item
        else:
            return item
        if not isinstance(request, IngestRequest):
            return item
        candidate_value = self._candidate_value(request.metadata)
        if not candidate_value:
            return item
        enriched = self._enrich_request(request, candidate_value)
        return {**container, "request": enriched} if container is not None else enriched

    def ingest_batch(
        self,
        invocation_id: str,
        operation: str,
        requests: list,
        *,
        research_run_external_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        return self.delegate.ingest_batch(
            invocation_id,
            operation,
            [self._enrich_batch_item(item) for item in requests],
            research_run_external_id=research_run_external_id,
            metadata=metadata,
        )

    def bounded_ingest_batch(
        self,
        invocation_id: str,
        operation: str,
        requests: list,
        *,
        research_run_external_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        return self.delegate.bounded_ingest_batch(
            invocation_id,
            operation,
            [self._enrich_batch_item(item) for item in requests],
            research_run_external_id=research_run_external_id,
            metadata=metadata,
        )


__all__ = ["TemporalCorpusService"]
