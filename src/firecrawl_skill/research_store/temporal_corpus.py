"""Carry search temporal provenance into direct-scrape corpus ingestion."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID


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
        update_value = (
            signals.get("updated_date") if isinstance(signals, dict) else None
        )
        metadata = dict(request.metadata)
        metadata["temporal_provenance"] = {
            "candidate_id": str(candidate_id),
            "published_at": (
                candidate["published_at"].isoformat()
                if candidate.get("published_at") is not None
                else None
            ),
            "updated_at": str(update_value) if update_value not in (None, "") else None,
            "retrieved_at": request.retrieved_at.isoformat(),
            "publication_authority": "explicit_provider_only",
            "retrieval_is_publication": False,
        }
        enriched = replace(
            request,
            published_at=request.published_at or candidate.get("published_at"),
            last_modified=request.last_modified
            or (str(update_value) if update_value not in (None, "") else None),
            metadata=metadata,
        )
        return self.delegate.prepare_ingest(enriched)


__all__ = ["TemporalCorpusService"]
