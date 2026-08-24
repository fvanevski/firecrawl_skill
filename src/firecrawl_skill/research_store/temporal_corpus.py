"""Carry explicit temporal provenance into corpus ingestion.

The facade enriches only requests that carry search-candidate provenance
(either the direct-scrape ``direct_scrape.candidate_id`` nesting or the
orchestrator's top-level ``candidate_id``); every other request is
forwarded to the delegate untouched.  Enrichment is the single canonical
implementation shared by the single- and batch-entry points, so the
orchestrator's bounded extraction path and direct scrape share it without
a second temporal normalization.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

from .domain import IngestRequest
from .temporal_candidate import (
    extract_document_temporal_signals,
    parse_provider_datetime,
)


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
        if isinstance(candidate_publication, str):
            candidate_publication = parse_provider_datetime(candidate_publication)
        document_publication = (
            parse_provider_datetime(document.get("published_at"))
            if document.get("publication_status") == "explicit_provider_valid"
            else None
        )
        publication = candidate_publication or document_publication
        publication_conflict = bool(
            candidate_publication is not None
            and document_publication is not None
            and candidate_publication != document_publication
        )
        if publication_conflict:
            publication = None

        candidate_update_raw = signals.get("updated_date")
        if not isinstance(candidate_update_raw, str):
            candidate_update_raw = None
        document_update = (
            parse_provider_datetime(document.get("updated_at"))
            if document.get("update_status") == "explicit_provider_valid"
            else None
        )
        update = candidate_update_raw or (
            document_update.isoformat() if document_update is not None else None
        )

        metadata = dict(request.metadata)
        metadata["temporal_provenance"] = {
            "candidate_id": str(candidate_id),
            "published_at": publication.isoformat()
            if publication is not None
            else None,
            "updated_at": update,
            "retrieved_at": request.retrieved_at.isoformat(),
            "publication_status": (
                "explicit_conflict"
                if publication_conflict
                else signals.get("publication_status")
                or document.get("publication_status")
                or "unknown"
            ),
            "update_status": (
                document.get("update_status")
                if document_update is not None
                else signals.get("update_status") or "unknown"
            ),
            "candidate_publication_signals": signals.get("publication_signals", []),
            "candidate_update_signals": signals.get("update_signals", []),
            "document_publication_signals": document.get("publication_signals", []),
            "document_update_signals": document.get("update_signals", []),
            "publication_authority": (
                "explicit_provider_only"
                if candidate_publication is not None
                else "explicit_signal_only"
            ),
            "update_authority": (
                "explicit_provider_only"
                if candidate_update_raw is not None
                else "explicit_signal_only"
            ),
            "retrieval_is_publication": False,
        }
        return replace(
            request,
            published_at=request.published_at or publication,
            last_modified=request.last_modified or update,
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
