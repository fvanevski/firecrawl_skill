"""Carry explicit temporal provenance into direct-scrape corpus ingestion."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

from .temporal_candidate import extract_document_temporal_signals, parse_provider_datetime

_VALID_EXPLICIT = {"explicit_provider_valid", "previous_explicit_provider"}


class TemporalCorpusService:
    """Narrow CorpusService facade for direct-scrape temporal propagation."""

    def __init__(self, delegate: Any, uow_factory: Any) -> None:
        self.delegate = delegate
        self.uow_factory = uow_factory

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def prepare_ingest(self, request: Any) -> Any:
        direct = request.metadata.get("direct_scrape", {}) if request.metadata else {}
        candidate_value = direct.get("candidate_id") if isinstance(direct, dict) else None
        if not candidate_value:
            return self.delegate.prepare_ingest(request)

        candidate_id = UUID(str(candidate_value))
        with self.uow_factory() as uow:
            candidate = uow.candidates.get_candidate(candidate_id)
        signals = candidate.get("date_signals") or {}
        if not isinstance(signals, dict):
            signals = {}
        transport = direct.get("transport") if isinstance(direct, dict) else None
        transport = transport if isinstance(transport, dict) else {}
        document = extract_document_temporal_signals(
            request.content,
            mime_type=request.mime_type,
            transport_metadata=transport,
        )

        candidate_publication = (
            candidate.get("published_at")
            if str(signals.get("publication_status")) in _VALID_EXPLICIT
            else None
        )
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

        candidate_update = (
            parse_provider_datetime(signals.get("updated_date"))
            if str(signals.get("update_status")) in _VALID_EXPLICIT
            else None
        )
        document_update = (
            parse_provider_datetime(document.get("updated_at"))
            if document.get("update_status") == "explicit_provider_valid"
            else None
        )
        update = document_update or candidate_update

        metadata = dict(request.metadata)
        metadata["temporal_provenance"] = {
            "candidate_id": str(candidate_id),
            "published_at": publication.isoformat() if publication is not None else None,
            "updated_at": update.isoformat() if update is not None else None,
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
            "publication_authority": "explicit_signal_only",
            "update_authority": "explicit_signal_only",
            "retrieval_is_publication": False,
        }
        enriched = replace(
            request,
            published_at=request.published_at or publication,
            last_modified=request.last_modified
            or (update.isoformat() if update is not None else None),
            metadata=metadata,
        )
        return self.delegate.prepare_ingest(enriched)


__all__ = ["TemporalCorpusService"]
