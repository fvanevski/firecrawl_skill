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

import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from .domain import IngestRequest
from .temporal_candidate import (
    extract_document_temporal_signals,
    parse_provider_datetime,
)
from .temporal_resolution import (
    MAX_TEMPORAL_PROVENANCE_PROBES_PER_RUN,
    resolve_document_temporal_provenance,
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

    def _resolve_document_provenance(
        self,
        *,
        candidate: dict[str, Any],
        request: IngestRequest,
        document: dict[str, Any],
        resolution_input_sha256: str,
    ) -> dict[str, Any]:
        """Run or replay one persisted bounded provenance-resolution pass."""

        run_value = candidate.get("run_id")
        if run_value in (None, ""):
            # Compatibility/test-double path: without durable run identity there
            # is no authority for the persisted run-scoped resolver. Canonical
            # document extraction still proceeds below without fabricating it.
            return {}
        run_id = UUID(str(run_value))
        content_sha256 = hashlib.sha256(request.content).hexdigest()
        candidate_id = str(candidate["id"])
        with self.uow_factory() as uow:
            events = uow.runs.list_events(
                run_id,
                event_type="temporal.provenance_resolution",
                limit=MAX_TEMPORAL_PROVENANCE_PROBES_PER_RUN,
                offset=0,
                for_update=True,
            )
            for event in events:
                payload = event.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                persisted_input_sha256 = payload.get("resolution_input_sha256")
                if persisted_input_sha256 in (None, ""):
                    persisted_input_sha256 = payload.get("content_sha256")
                if (
                    str(payload.get("candidate_id") or "") == candidate_id
                    and persisted_input_sha256 == resolution_input_sha256
                    and isinstance(payload.get("resolution"), dict)
                ):
                    return dict(payload["resolution"])

            ordinal = len(events) + 1
            resolution = resolve_document_temporal_provenance(
                document,
                retrieved_at=request.retrieved_at,
                run_probe_ordinal=ordinal,
            )
            if resolution.get("attempted") is True:
                payload = {
                    "candidate_id": candidate_id,
                    "content_sha256": content_sha256,
                    "resolution_input_sha256": resolution_input_sha256,
                    "resolution": resolution,
                }
                uow.runs.append_event(
                    run_id,
                    "temporal.provenance_resolution",
                    "system",
                    (
                        "temporal-provenance-resolution:"
                        f"{candidate_id}:{resolution_input_sha256}"
                    ),
                    actor_identifier="TemporalCorpusService",
                    payload=payload,
                )
                uow.commit()
            return resolution

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
            parsed.append((source, normalized.astimezone(timezone.utc)))

        if invalid:
            return None, "explicit_invalid", "none"
        distinct = {value for _, value in parsed}
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
        firecrawl = request.metadata.get("firecrawl") if request.metadata else None
        source_context = {
            "requested_url": request.requested_url,
            "final_url": request.final_url,
            "source_url": firecrawl.get("source_url")
            if isinstance(firecrawl, dict)
            else None,
        }
        document = extract_document_temporal_signals(
            request.content,
            mime_type=request.mime_type,
            transport_metadata=transport,
            source_context=source_context,
        )
        metadata = dict(request.metadata)
        raw_sidecar = metadata.pop("_temporal_provenance_sidecar", None)
        sidecar_document: dict[str, Any] = {}
        sidecar_descriptor: dict[str, Any] = {}
        sidecar_bytes = b""
        sidecar_mime_type = ""
        if isinstance(raw_sidecar, dict):
            sidecar_content = raw_sidecar.get("content")
            sidecar_mime_type = str(raw_sidecar.get("mime_type") or "").strip()
            if isinstance(sidecar_content, str):
                sidecar_bytes = sidecar_content.encode("utf-8")
            elif isinstance(sidecar_content, bytes):
                sidecar_bytes = sidecar_content
            if sidecar_bytes and sidecar_mime_type:
                sidecar_document = extract_document_temporal_signals(
                    sidecar_bytes,
                    mime_type=sidecar_mime_type,
                    source_context=source_context,
                )
                sidecar_descriptor = {
                    "source": str(raw_sidecar.get("source") or "provider_sidecar"),
                    "mime_type": sidecar_mime_type,
                    "sha256": hashlib.sha256(sidecar_bytes).hexdigest(),
                    "byte_length": len(sidecar_bytes),
                }

        combined_document = {
            **document,
            "publication_signals": [
                *(document.get("publication_signals") or []),
                *(sidecar_document.get("publication_signals") or []),
            ],
            "update_signals": [
                *(document.get("update_signals") or []),
                *(sidecar_document.get("update_signals") or []),
            ],
            "structured_temporal_segments": [
                *(document.get("structured_temporal_segments") or []),
                *(sidecar_document.get("structured_temporal_segments") or []),
            ],
        }
        resolution_hasher = hashlib.sha256()
        resolution_hasher.update(request.content)
        if sidecar_bytes:
            resolution_hasher.update(b"\0temporal-provenance-sidecar\0")
            resolution_hasher.update(sidecar_mime_type.encode("utf-8"))
            resolution_hasher.update(b"\0")
            resolution_hasher.update(sidecar_bytes)
        resolution_input_sha256 = resolution_hasher.hexdigest()
        resolution = self._resolve_document_provenance(
            candidate=candidate,
            request=request,
            document=combined_document,
            resolution_input_sha256=resolution_input_sha256,
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
                    (
                        "document",
                        sidecar_document.get("published_at"),
                        str(sidecar_document.get("publication_status") or "unknown"),
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
                (
                    "document",
                    sidecar_document.get("updated_at"),
                    str(sidecar_document.get("update_status") or "unknown"),
                ),
            ]
        )

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
            "sidecar_publication_status": sidecar_document.get(
                "publication_status", "unknown"
            ),
            "sidecar_update_status": sidecar_document.get("update_status", "unknown"),
            "sidecar_publication_signals": sidecar_document.get(
                "publication_signals", []
            ),
            "sidecar_update_signals": sidecar_document.get("update_signals", []),
            "temporal_sidecar": sidecar_descriptor,
            "structured_temporal_segments": combined_document.get(
                "structured_temporal_segments", []
            ),
            "publication_authority": publication_authority,
            "update_authority": update_authority,
            "source_semantics": document.get("source_semantics") or {},
            "event_at": resolution.get("event_at"),
            "event_status": resolution.get("event_status") or "unknown",
            "event_authority": resolution.get("event_authority") or "none",
            "state_observed_at": resolution.get("state_observed_at"),
            "state_authority": resolution.get("state_authority") or "none",
            "resolution": resolution,
            "retrieval_is_publication": False,
            "retrieval_is_update": False,
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
        if container is None:
            return enriched
        return {
            **container,
            "request": enriched,
            "metadata": dict(enriched.metadata),
        }

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
