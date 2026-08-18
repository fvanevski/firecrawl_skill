"""PostgreSQL-authoritative direct scrape application service.

The service performs fail-closed authority checks before constructing or
invoking a transport adapter. Provider/network mechanics are owned by
``acquisition.adapters``; this module owns deterministic persistence,
idempotency, provenance, retry, and failure policy.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from ..config import StoreConfig
from ..derivation_service import _configuration_sha256
from ..domain import IngestRequest
from ..postgres import IndexingPersistenceError
from ..url import canonicalize_candidate_url
from .authority import (
    ACQUISITION_ENTRY_STATES,
    AuthoritativeAcquisitionContext,
    require_authoritative_acquisition,
)
from .models import (
    DirectScrapeBatchResult,
    DirectScrapeItemResult,
    DirectScrapeRequest,
    ScrapeTransportResult,
)
from .ports import DirectScrapeAdapter

_MAX_DIAGNOSTIC_CHARS = 500

DIRECT_SCRAPE_TABLE_PRIVILEGES: Mapping[str, frozenset[str]] = {
    "research_runs": frozenset({"SELECT", "UPDATE"}),
    "research_invocations": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "research_events": frozenset({"SELECT", "INSERT"}),
    "search_candidates": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "extraction_attempts": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "sources": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "asset_snapshots": frozenset({"SELECT", "INSERT"}),
    "documents": frozenset({"SELECT", "INSERT"}),
    "document_blocks": frozenset({"SELECT", "INSERT"}),
    "chunks": frozenset({"SELECT", "INSERT"}),
    "index_definitions": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "embedding_manifests": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "index_jobs": frozenset({"SELECT", "INSERT"}),
    "document_derivations": frozenset({"SELECT", "INSERT"}),
    "research_run_assets": frozenset({"SELECT", "INSERT", "UPDATE"}),
}


class DirectScrapeError(RuntimeError):
    """Base error for authoritative direct scrape execution."""


class DirectScrapePersistenceError(DirectScrapeError):
    """A typed authoritative persistence failure."""

    def __init__(self, message: str, *, stage: str = "ingestion") -> None:
        if stage not in {"ingestion", "indexing"}:
            raise ValueError(f"invalid direct-scrape persistence stage: {stage}")
        super().__init__(message)
        self.stage = stage


def require_direct_scrape_persistence(uow_factory: Callable[[], Any]) -> None:
    """Verify every table privilege used by direct scrape before transport."""
    missing: list[str] = []
    with uow_factory() as uow, uow.connection.cursor() as cur:
        for table, privileges in DIRECT_SCRAPE_TABLE_PRIVILEGES.items():
            for privilege in sorted(privileges):
                cur.execute(
                    "SELECT has_table_privilege(current_user, %s, %s)",
                    (table, privilege),
                )
                row = cur.fetchone()
                if not row or row[0] is not True:
                    missing.append(f"{table}:{privilege}")
    if missing:
        raise DirectScrapePersistenceError(
            "authoritative PostgreSQL role lacks direct-scrape privileges: "
            + ", ".join(missing)
        )


@dataclass(frozen=True)
class _ResolvedTarget:
    index: int
    item_key: str
    request: DirectScrapeRequest
    candidate_id: UUID
    requested_url: str
    canonical_url: str
    title: str | None
    retry_parent_id: UUID | None = None


class DirectScrapeService:
    """Execute direct scrapes with PostgreSQL/blob authority and resumable IDs."""

    def __init__(
        self,
        config: StoreConfig,
        uow_factory: Callable[[], Any],
        blob_store: Any,
        corpus_service: Any,
        *,
        adapter_factory: Callable[[], DirectScrapeAdapter],
        preflight: Callable[..., AuthoritativeAcquisitionContext] = (
            require_authoritative_acquisition
        ),
        authority_check: Callable[[Callable[[], Any]], None] = (
            require_direct_scrape_persistence
        ),
        queue: Any = None,
    ) -> None:
        self.config = config
        self.uow_factory = uow_factory
        self.blob_store = blob_store
        self.corpus_service = corpus_service
        self.adapter_factory = adapter_factory
        self.preflight = preflight
        self.authority_check = authority_check
        self.queue = queue

    def execute(
        self,
        run_id: UUID | str,
        requests: Sequence[DirectScrapeRequest],
        *,
        idempotency_key: str | None = None,
        external_invocation_id: str | None = None,
        parent_invocation_id: UUID | str | None = None,
        retry_parent_attempt_ids: Mapping[int, UUID | str] | None = None,
    ) -> DirectScrapeBatchResult:
        if not requests:
            raise ValueError("at least one scrape request is required")
        run_uuid = UUID(str(run_id))
        context = self.preflight(run_id=run_uuid, config=self.config)
        self.authority_check(self.uow_factory)

        normalized = tuple(self._normalize_request(item) for item in requests)
        parent_uuid = (
            UUID(str(parent_invocation_id))
            if parent_invocation_id is not None
            else None
        )
        retry_parents = {
            int(index): UUID(str(attempt_id))
            for index, attempt_id in (retry_parent_attempt_ids or {}).items()
        }
        if any(index < 0 or index >= len(normalized) for index in retry_parents):
            raise ValueError("retry parent index is outside the request batch")
        batch_key = idempotency_key or self._default_idempotency_key(
            run_uuid, normalized
        )
        if not batch_key.strip():
            raise ValueError("idempotency_key must be non-empty")

        candidates = self._resolve_existing_candidates(run_uuid, normalized)
        invocation_id, saved_items, terminal_status = self._begin_or_resume(
            context,
            normalized,
            candidates,
            batch_key,
            external_invocation_id,
            parent_uuid,
        )
        if terminal_status is not None:
            items = tuple(
                DirectScrapeItemResult.from_mapping(item)
                for item in sorted(saved_items.values(), key=lambda item: item["index"])
            )
            return DirectScrapeBatchResult(
                run_id=run_uuid,
                invocation_id=invocation_id,
                idempotency_key=batch_key,
                status=terminal_status,
                items=items,
                replayed=True,
            )

        # The adapter is intentionally constructed only after both authority
        # preflight and direct-scrape privilege validation succeed.
        adapter = self.adapter_factory()
        resolved = self._load_resolved_targets(
            run_uuid,
            invocation_id,
            normalized,
            candidates,
            batch_key,
            retry_parents,
        )
        results: dict[str, DirectScrapeItemResult] = {
            key: DirectScrapeItemResult.from_mapping(value)
            for key, value in saved_items.items()
        }

        for target in resolved:
            if target.item_key in results:
                continue
            with self._claim_item(context, invocation_id, target.item_key) as existing:
                if existing is not None:
                    results[target.item_key] = DirectScrapeItemResult.from_mapping(
                        existing
                    )
                    continue
                transport = adapter.scrape(
                    target.canonical_url,
                    format=target.request.effective_format,
                    summary=target.request.effective_summary,
                    schema=target.request.schema,
                    options=target.request.options,
                )
                if transport.succeeded:
                    result = self._persist_success(
                        context, invocation_id, target, transport
                    )
                else:
                    result = self._persist_failure(
                        context, invocation_id, target, transport
                    )
                results[target.item_key] = result

        ordered = tuple(sorted(results.values(), key=lambda item: item.index))
        status = self._batch_status(ordered)
        self._finalize_invocation(context, invocation_id, batch_key, status, ordered)
        return DirectScrapeBatchResult(
            run_id=run_uuid,
            invocation_id=invocation_id,
            idempotency_key=batch_key,
            status=status,
            items=ordered,
        )

    def retry_failed(
        self,
        run_id: UUID | str,
        requests: Sequence[DirectScrapeRequest],
        *,
        prior_invocation_id: UUID | str,
        idempotency_key: str,
    ) -> DirectScrapeBatchResult:
        """Retry only failed items with explicit invocation and attempt lineage."""
        if not idempotency_key.strip():
            raise ValueError("retry idempotency_key must be non-empty")
        run_uuid = UUID(str(run_id))
        prior_uuid = UUID(str(prior_invocation_id))
        self.preflight(run_id=run_uuid, config=self.config)
        self.authority_check(self.uow_factory)
        normalized = tuple(self._normalize_request(item) for item in requests)
        expected_input = self._invocation_input(normalized)
        with self.uow_factory() as uow, uow.connection.cursor() as cur:
            cur.execute(
                """SELECT status,idempotency_key,input,output
                    FROM research_invocations
                    WHERE id=%s AND run_id=%s AND operation='direct_scrape'""",
                (prior_uuid, run_uuid),
            )
            row = cur.fetchone()
        if row is None:
            raise DirectScrapePersistenceError(
                f"prior direct scrape invocation not found: {prior_uuid}"
            )
        status, prior_key, stored_input, output = row
        if status not in {"partial", "failed"}:
            raise DirectScrapePersistenceError(
                "only partial or failed direct scrape invocations can be retried"
            )
        if prior_key == idempotency_key:
            raise ValueError("retry requires a new idempotency_key")
        if stored_input != expected_input:
            raise DirectScrapePersistenceError(
                "retry requests do not match the prior invocation input"
            )

        failed = [
            item
            for item in sorted(
                self._items_by_key(output).values(),
                key=lambda value: value["index"],
            )
            if item.get("status") == "failed"
        ]
        if not failed:
            raise DirectScrapePersistenceError(
                "prior invocation has no failed items to retry"
            )

        retry_requests: list[DirectScrapeRequest] = []
        retry_parents: dict[int, UUID] = {}
        for retry_index, item in enumerate(failed):
            original_index = int(item["index"])
            attempt_id = item.get("extraction_attempt_id")
            if attempt_id is None:
                raise DirectScrapePersistenceError(
                    "failed item has no extraction-attempt identity"
                )
            retry_requests.append(normalized[original_index])
            retry_parents[retry_index] = UUID(str(attempt_id))

        return self.execute(
            run_uuid,
            retry_requests,
            idempotency_key=idempotency_key,
            parent_invocation_id=prior_uuid,
            retry_parent_attempt_ids=retry_parents,
        )

    @contextmanager
    def _claim_item(
        self,
        context: AuthoritativeAcquisitionContext,
        invocation_id: UUID,
        item_key: str,
    ) -> Iterator[dict[str, Any] | None]:
        """Serialize one provider execution with a session advisory lock."""
        lock_key = self._advisory_lock_key(invocation_id, item_key)
        with self.uow_factory() as uow:
            with uow.connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
            uow.connection.commit()
            try:
                self._revalidate_run(uow, context)
                with uow.connection.cursor() as cur:
                    cur.execute(
                        """SELECT status,output FROM research_invocations
                        WHERE id=%s AND run_id=%s""",
                        (invocation_id, self._require_context_run(context)),
                    )
                    row = cur.fetchone()
                uow.connection.commit()
                if row is None:
                    raise DirectScrapePersistenceError(
                        "direct scrape invocation disappeared before item claim"
                    )
                status, output = row
                existing = self._items_by_key(output).get(item_key)
                if existing is None and status not in {"pending", "running"}:
                    raise DirectScrapePersistenceError(
                        f"direct scrape invocation became terminal before item claim: {status}"
                    )
                yield existing
            finally:
                with uow.connection.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
                uow.connection.commit()

    @staticmethod
    def _advisory_lock_key(invocation_id: UUID, item_key: str) -> int:
        digest = hashlib.sha256(f"{invocation_id}:{item_key}".encode()).digest()
        value = int.from_bytes(digest[:8], "big", signed=False)
        return value - (1 << 64) if value >= (1 << 63) else value

    @staticmethod
    def _normalize_request(request: DirectScrapeRequest) -> DirectScrapeRequest:
        schema = dict(request.schema) if request.schema is not None else None
        options = dict(request.options)
        candidate_id = (
            UUID(str(request.candidate_id))
            if request.candidate_id is not None
            else None
        )
        return replace(
            request,
            url=request.url.strip() if request.url is not None else None,
            candidate_id=candidate_id,
            schema=schema,
            options=options,
        )

    @staticmethod
    def _default_idempotency_key(
        run_id: UUID, requests: Sequence[DirectScrapeRequest]
    ) -> str:
        payload = [
            {
                "url": item.url,
                "candidate_id": str(item.candidate_id) if item.candidate_id else None,
                "format": item.effective_format,
                "summary": item.effective_summary,
                "schema": item.schema,
                "mime_type": item.effective_mime_type,
                "options": item.options,
            }
            for item in requests
        ]
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return f"direct-scrape:{run_id}:{digest}"

    @staticmethod
    def _item_key(
        batch_key: str, index: int, request: DirectScrapeRequest, canonical_url: str
    ) -> str:
        payload = {
            "batch": batch_key,
            "index": index,
            "candidate_id": str(request.candidate_id) if request.candidate_id else None,
            "canonical_url": canonical_url,
            "format": request.effective_format,
            "summary": request.summary,
            "schema": request.schema,
            "mime_type": request.effective_mime_type,
            "options": request.options,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _resolve_existing_candidates(
        self, run_id: UUID, requests: Sequence[DirectScrapeRequest]
    ) -> dict[int, dict[str, Any]]:
        resolved: dict[int, dict[str, Any]] = {}
        with self.uow_factory() as uow:
            for index, request in enumerate(requests):
                if request.candidate_id is not None:
                    candidate = uow.candidates.get_candidate(
                        request.candidate_id, run_id=run_id
                    )
                    resolved[index] = candidate
                    continue
                canonical_url, _original_url = canonicalize_candidate_url(
                    request.url or ""
                )
                canonical_sha = hashlib.sha256(canonical_url.encode()).hexdigest()
                with uow.connection.cursor() as cur:
                    cur.execute(
                        """SELECT id,canonical_url,original_url,title
                        FROM search_candidates
                        WHERE run_id=%s AND canonical_url_sha256=%s""",
                        (run_id, canonical_sha),
                    )
                    row = cur.fetchone()
                if row is not None:
                    resolved[index] = {
                        "id": row[0],
                        "canonical_url": row[1],
                        "original_url": row[2],
                        "title": row[3],
                    }
        return resolved

    def _begin_or_resume(
        self,
        context: AuthoritativeAcquisitionContext,
        requests: Sequence[DirectScrapeRequest],
        candidates: dict[int, dict[str, Any]],
        idempotency_key: str,
        external_invocation_id: str | None,
        parent_invocation_id: UUID | None,
    ) -> tuple[UUID, dict[str, dict[str, Any]], str | None]:
        run_id = self._require_context_run(context)
        input_payload = self._invocation_input(requests)
        with self.uow_factory() as uow:
            lifecycle_state, lifecycle_revision = self._revalidate_run(uow, context)
            with uow.connection.cursor() as cur:
                cur.execute(
                    """SELECT id,status,input,output FROM research_invocations
                    WHERE run_id=%s AND idempotency_key=%s FOR UPDATE""",
                    (run_id, idempotency_key),
                )
                row = cur.fetchone()
                if row is not None:
                    invocation_id, status, stored_input, output = row
                    if stored_input != input_payload:
                        raise DirectScrapePersistenceError(
                            "idempotency key was used for another direct scrape request"
                        )
                    items = self._items_by_key(output)
                    if status == "cancelled":
                        raise DirectScrapePersistenceError(
                            "cancelled direct scrape invocations cannot be resumed"
                        )
                    terminal = (
                        status if status in {"complete", "partial", "failed"} else None
                    )
                    return invocation_id, items, terminal

                cur.execute(
                    """INSERT INTO research_invocations(
                    run_id,parent_invocation_id,external_invocation_id,operation,
                    status,lifecycle_revision,idempotency_key,input,output,metadata,
                    started_at)
                    VALUES(%s,%s,%s,'direct_scrape','running',%s,%s,%s,%s,%s,now())
                    RETURNING id""",
                    (
                        run_id,
                        parent_invocation_id,
                        external_invocation_id,
                        lifecycle_revision,
                        idempotency_key,
                        json.dumps(input_payload),
                        json.dumps({"schema_version": "direct-scrape-v1", "items": []}),
                        json.dumps(
                            {
                                "authority": "postgresql",
                                "payload_store": "blob",
                                "lifecycle_state": lifecycle_state,
                                "lifecycle_revision": lifecycle_revision,
                                "retry_of_invocation_id": (
                                    str(parent_invocation_id)
                                    if parent_invocation_id is not None
                                    else None
                                ),
                            }
                        ),
                    ),
                )
                invocation_id = cur.fetchone()[0]

                for index, request in enumerate(requests):
                    if request.url is None:
                        continue
                    canonical_url, original_url = canonicalize_candidate_url(
                        request.url
                    )
                    canonical_sha = hashlib.sha256(canonical_url.encode()).hexdigest()
                    domain = urlsplit(canonical_url).hostname or "unknown"
                    cur.execute(
                        """INSERT INTO search_candidates(
                        run_id,canonical_url,canonical_url_sha256,original_url,
                        domain,backend,backend_metadata)
                        VALUES(%s,%s,%s,%s,%s,'direct_scrape',%s)
                        ON CONFLICT(run_id,canonical_url_sha256) DO UPDATE
                          SET last_seen_at=now(),
                              backend_metadata=(
                                search_candidates.backend_metadata
                                || excluded.backend_metadata
                              )
                        RETURNING id,canonical_url,original_url,title""",
                        (
                            run_id,
                            canonical_url,
                            canonical_sha,
                            original_url,
                            domain,
                            json.dumps({"direct_input_index": index}),
                        ),
                    )
                    candidate_id, stored_url, stored_original, title = cur.fetchone()
                    candidates[index] = {
                        "id": candidate_id,
                        "canonical_url": stored_url,
                        "original_url": stored_original,
                        "title": title,
                    }

            uow.runs.append_event(
                run_id,
                "direct_scrape_started",
                "system",
                f"{idempotency_key}:started",
                invocation_id=invocation_id,
                payload={
                    "item_count": len(requests),
                    "lifecycle_state": lifecycle_state,
                    "lifecycle_revision": lifecycle_revision,
                },
            )
            return invocation_id, {}, None

    @staticmethod
    def _invocation_input(
        requests: Sequence[DirectScrapeRequest],
    ) -> dict[str, Any]:
        return {
            "schema_version": "direct-scrape-v1",
            "requests": [
                {
                    "url": item.url,
                    "candidate_id": str(item.candidate_id)
                    if item.candidate_id is not None
                    else None,
                    "format": item.effective_format,
                    "summary": item.effective_summary,
                    "schema": item.schema,
                    "mime_type": item.effective_mime_type,
                    "options": item.options,
                }
                for item in requests
            ],
        }

    def _load_resolved_targets(
        self,
        run_id: UUID,
        invocation_id: UUID,
        requests: Sequence[DirectScrapeRequest],
        candidates: Mapping[int, Mapping[str, Any]],
        batch_key: str,
        retry_parent_attempt_ids: Mapping[int, UUID],
    ) -> tuple[_ResolvedTarget, ...]:
        resolved: list[_ResolvedTarget] = []
        for index, request in enumerate(requests):
            candidate = candidates[index]
            canonical_url = str(candidate["canonical_url"])
            requested_url = request.url or str(
                candidate.get("original_url") or canonical_url
            )
            resolved.append(
                _ResolvedTarget(
                    index=index,
                    item_key=self._item_key(batch_key, index, request, canonical_url),
                    request=request,
                    candidate_id=UUID(str(candidate["id"])),
                    requested_url=requested_url,
                    canonical_url=canonical_url,
                    title=candidate.get("title"),
                    retry_parent_id=retry_parent_attempt_ids.get(index),
                )
            )
        return tuple(resolved)

    def _persist_success(
        self,
        context: AuthoritativeAcquisitionContext,
        invocation_id: UUID,
        target: _ResolvedTarget,
        transport: ScrapeTransportResult,
    ) -> DirectScrapeItemResult:
        run_id = self._require_context_run(context)
        raw_ref = self.blob_store.put(
            io.BytesIO(transport.raw_payload), target.request.effective_mime_type
        )
        request = IngestRequest(
            requested_url=target.requested_url,
            final_url=transport.final_url or target.canonical_url,
            content=transport.raw_payload,
            normalized_content=transport.raw_payload,
            mime_type=target.request.effective_mime_type,
            title=transport.title or target.title,
            retrieved_at=transport.responded_at,
            http_status=transport.http_status,
            firecrawl_version=str(
                transport.metadata.get("firecrawl_cli_version") or "unknown"
            ),
            crawl_options={
                "format": target.request.effective_format,
                "summary": target.request.effective_summary,
                "schema": target.request.schema,
                "options": target.request.options,
            },
            metadata={
                "direct_scrape": {
                    "candidate_id": str(target.candidate_id),
                    "invocation_id": str(invocation_id),
                    "provider_request_id": transport.provider_request_id,
                    "transport": self._bounded_mapping(transport.metadata),
                }
            },
        )
        try:
            prepared = self.corpus_service.prepare_ingest(request)
        except Exception as exc:  # noqa: BLE001
            failed = replace(
                transport,
                returncode=1,
                stderr=f"parser failure: {exc}".encode(),
                metadata={**dict(transport.metadata), "failure_class": "parser"},
            )
            return self._persist_failure(
                context, invocation_id, target, failed, raw_ref=raw_ref
            )

        try:
            with self.uow_factory() as uow:
                self._revalidate_run(uow, context)
                existing = self._existing_item(uow, invocation_id, target.item_key)
                if existing is not None:
                    return DirectScrapeItemResult.from_mapping(existing)
                attempt_number = self._next_attempt_number(uow, target.candidate_id)
                attempt_id = uow.extraction_attempts.create_attempt(
                    candidate_id=target.candidate_id,
                    run_id=run_id,
                    invocation_id=invocation_id,
                    attempt_number=attempt_number,
                    method=self._method_for(target.request),
                    method_version="firecrawl-cli-direct-v1",
                    requested_format=target.request.effective_format,
                    start_time=transport.requested_at,
                    end_time=transport.responded_at,
                    exit_status="succeeded",
                    http_status=transport.http_status,
                    backend_status="complete",
                    raw_blob=raw_ref,
                    normalized_blob=prepared.blob,
                    parser_used=(
                        f"{prepared.parser_name}@"
                        f"{prepared.parser_implementation_version}"
                    ),
                    quality_metrics=None,
                    failure_class="none",
                    retry_parent_id=target.retry_parent_id,
                    disposition="acceptable",
                    error_message=None,
                    selection_reason="authoritative direct scrape",
                )
                prepared_request = replace(
                    prepared.request, extraction_attempt_id=attempt_id
                )
                prepared = replace(prepared, request=prepared_request)
                ingest = uow.persist_ingest(*prepared.persist_args())
                parser_identity = (
                    f"{prepared.parser_name}@"
                    f"{prepared.parser_implementation_version}:"
                    f"{prepared.parser_version}"
                )
                configuration_sha = _configuration_sha256(
                    parser_identity,
                    prepared.normalization_version,
                    prepared.chunker_name,
                    prepared.chunker_version,
                    self.config.tokenizer_name,
                )
                derivation = uow.derivations.find_by_configuration(
                    ingest.document_id, configuration_sha
                )
                if derivation is None:
                    derivation_model = uow.derivations.create(
                        document_id=ingest.document_id,
                        snapshot_id=ingest.snapshot_id,
                        parser_version=parser_identity,
                        normalization_version=prepared.normalization_version,
                        chunker_name=prepared.chunker_name,
                        chunker_version=prepared.chunker_version,
                        tokenizer_name=self.config.tokenizer_name,
                        chunk_count=len(ingest.chunk_ids),
                        block_count=len(prepared.blocks),
                        configuration_sha256=configuration_sha,
                        status="active",
                    )
                    derivation_id = derivation_model.id
                else:
                    derivation_id = UUID(str(derivation["id"]))
                uow.extraction_attempts.select_final_attempt(
                    target.candidate_id,
                    attempt_id,
                    "authoritative direct scrape",
                )
                self._link_run_asset(
                    uow, run_id, ingest.snapshot_id, invocation_id, target
                )
                result = DirectScrapeItemResult(
                    index=target.index,
                    item_key=target.item_key,
                    status="succeeded",
                    requested_url=target.requested_url,
                    canonical_url=target.canonical_url,
                    candidate_id=target.candidate_id,
                    invocation_id=invocation_id,
                    format=target.request.effective_format,
                    mime_type=target.request.effective_mime_type,
                    extraction_attempt_id=attempt_id,
                    source_id=ingest.source_id,
                    snapshot_id=ingest.snapshot_id,
                    document_id=ingest.document_id,
                    derivation_id=derivation_id,
                    chunk_ids=ingest.chunk_ids,
                    content_sha256=ingest.content_sha256,
                    raw_blob_sha256=raw_ref.sha256,
                    reused_snapshot=ingest.reused_snapshot,
                    reused_document=ingest.reused_document,
                    reused_chunks=ingest.reused_chunks,
                )
                self._record_transport_event(
                    uow,
                    run_id,
                    invocation_id,
                    attempt_id,
                    target,
                    transport,
                    "succeeded",
                )
                self._record_item(uow, run_id, invocation_id, result)
            self._notify_jobs(result.chunk_ids)
            return result
        except IndexingPersistenceError as exc:
            raise DirectScrapePersistenceError(
                f"authoritative direct scrape indexing persistence failed: {exc}",
                stage="indexing",
            ) from exc
        except Exception as exc:
            raise DirectScrapePersistenceError(
                f"authoritative direct scrape ingestion persistence failed: {exc}",
                stage="ingestion",
            ) from exc

    def _persist_failure(
        self,
        context: AuthoritativeAcquisitionContext,
        invocation_id: UUID,
        target: _ResolvedTarget,
        transport: ScrapeTransportResult,
        *,
        raw_ref: Any = None,
    ) -> DirectScrapeItemResult:
        run_id = self._require_context_run(context)
        if raw_ref is None and transport.raw_payload:
            raw_ref = self.blob_store.put(
                io.BytesIO(transport.raw_payload), target.request.effective_mime_type
            )
        diagnostic = self._diagnostic(transport.stderr)
        failure_class = str(transport.metadata.get("failure_class") or "internal")
        if failure_class not in {
            "timeout",
            "network",
            "http_error",
            "parser",
            "schema_validation",
            "empty_content",
            "anti_bot",
            "unsupported_format",
            "blocked",
            "content_too_small",
            "content_too_large",
            "malformed",
            "internal",
        }:
            failure_class = "internal"
        if transport.returncode == 0 and not transport.raw_payload:
            failure_class = "empty_content"
            diagnostic = diagnostic or "provider returned an empty payload"

        with self.uow_factory() as uow:
            self._revalidate_run(uow, context)
            existing = self._existing_item(uow, invocation_id, target.item_key)
            if existing is not None:
                return DirectScrapeItemResult.from_mapping(existing)
            attempt_id = uow.extraction_attempts.create_attempt(
                candidate_id=target.candidate_id,
                run_id=run_id,
                invocation_id=invocation_id,
                attempt_number=self._next_attempt_number(uow, target.candidate_id),
                method=self._method_for(target.request),
                method_version="firecrawl-cli-direct-v1",
                requested_format=target.request.effective_format,
                start_time=transport.requested_at,
                end_time=transport.responded_at,
                exit_status="failed",
                http_status=transport.http_status,
                backend_status=f"exit:{transport.returncode}",
                raw_blob=raw_ref,
                normalized_blob=None,
                parser_used=None,
                quality_metrics=None,
                failure_class=failure_class,
                retry_parent_id=target.retry_parent_id,
                disposition="unassessed",
                error_message=diagnostic,
                selection_reason=None,
            )
            result = DirectScrapeItemResult(
                index=target.index,
                item_key=target.item_key,
                status="failed",
                requested_url=target.requested_url,
                canonical_url=target.canonical_url,
                candidate_id=target.candidate_id,
                invocation_id=invocation_id,
                format=target.request.effective_format,
                mime_type=target.request.effective_mime_type,
                extraction_attempt_id=attempt_id,
                raw_blob_sha256=raw_ref.sha256 if raw_ref is not None else None,
                error=diagnostic or "Firecrawl scrape failed",
                diagnostic=diagnostic,
                failure_class=failure_class,
            )
            self._record_transport_event(
                uow,
                run_id,
                invocation_id,
                attempt_id,
                target,
                transport,
                "failed",
            )
            self._record_item(uow, run_id, invocation_id, result)
            return result

    def _finalize_invocation(
        self,
        context: AuthoritativeAcquisitionContext,
        invocation_id: UUID,
        idempotency_key: str,
        status: str,
        items: Sequence[DirectScrapeItemResult],
    ) -> None:
        run_id = self._require_context_run(context)
        output = {
            "schema_version": "direct-scrape-v1",
            "items": [item.to_dict() for item in items],
        }
        with self.uow_factory() as uow:
            self._revalidate_run(uow, context)
            with uow.connection.cursor() as cur:
                cur.execute(
                    """UPDATE research_invocations
                    SET status=%s,output=%s,error=%s,completed_at=now()
                    WHERE id=%s AND run_id=%s AND status IN ('running','pending')
                    RETURNING id""",
                    (
                        status,
                        json.dumps(output),
                        (
                            "one or more direct scrapes failed"
                            if status != "complete"
                            else None
                        ),
                        invocation_id,
                        run_id,
                    ),
                )
                if cur.fetchone() is None:
                    cur.execute(
                        (
                            "SELECT status,input FROM research_invocations "
                            "WHERE id=%s AND run_id=%s"
                        ),
                        (invocation_id, run_id),
                    )
                    row = cur.fetchone()
                    if row is None or row[0] != status:
                        raise DirectScrapePersistenceError(
                            "direct scrape invocation could not be finalized"
                        )
            uow.runs.append_event(
                run_id,
                "direct_scrape_finished",
                "system",
                f"{idempotency_key}:finished",
                invocation_id=invocation_id,
                payload={
                    "status": status,
                    "succeeded": sum(item.status == "succeeded" for item in items),
                    "failed": sum(item.status == "failed" for item in items),
                },
            )

    def _record_transport_event(
        self,
        uow: Any,
        run_id: UUID,
        invocation_id: UUID,
        attempt_id: UUID,
        target: _ResolvedTarget,
        transport: ScrapeTransportResult,
        status: str,
    ) -> None:
        duration_ms = max(
            0,
            int(
                (transport.responded_at - transport.requested_at).total_seconds() * 1000
            ),
        )
        uow.runs.append_event(
            run_id,
            "direct_scrape_transport_recorded",
            "system",
            f"direct-scrape-transport:{attempt_id}",
            invocation_id=invocation_id,
            payload={
                "extraction_attempt_id": str(attempt_id),
                "candidate_id": str(target.candidate_id),
                "status": status,
                "format": target.request.effective_format,
                "mime_type": target.request.effective_mime_type,
                "requested_at": transport.requested_at.isoformat(),
                "responded_at": transport.responded_at.isoformat(),
                "duration_ms": duration_ms,
                "returncode": transport.returncode,
                "http_status": transport.http_status,
                "final_url": transport.final_url,
                "provider_request_id": transport.provider_request_id,
                "diagnostic": self._diagnostic(transport.stderr),
                "transport": self._bounded_mapping(transport.metadata),
            },
        )

    def _record_item(
        self,
        uow: Any,
        run_id: UUID,
        invocation_id: UUID,
        result: DirectScrapeItemResult,
    ) -> None:
        with uow.connection.cursor() as cur:
            cur.execute(
                (
                    "SELECT output FROM research_invocations "
                    "WHERE id=%s AND run_id=%s FOR UPDATE"
                ),
                (invocation_id, run_id),
            )
            row = cur.fetchone()
            if row is None:
                raise DirectScrapePersistenceError(
                    "direct scrape invocation disappeared"
                )
            output = row[0] or {"schema_version": "direct-scrape-v1", "items": []}
            items = self._items_by_key(output)
            items.setdefault(result.item_key, result.to_dict())
            ordered = sorted(items.values(), key=lambda item: item["index"])
            cur.execute(
                "UPDATE research_invocations SET output=%s WHERE id=%s AND run_id=%s",
                (
                    json.dumps(
                        {"schema_version": "direct-scrape-v1", "items": ordered}
                    ),
                    invocation_id,
                    run_id,
                ),
            )
        uow.runs.append_event(
            run_id,
            "direct_scrape_item_finished",
            "system",
            f"direct-scrape:{invocation_id}:{result.item_key}",
            invocation_id=invocation_id,
            payload={
                "index": result.index,
                "status": result.status,
                "candidate_id": str(result.candidate_id),
                "snapshot_id": str(result.snapshot_id) if result.snapshot_id else None,
                "document_id": str(result.document_id) if result.document_id else None,
                "extraction_attempt_id": str(result.extraction_attempt_id)
                if result.extraction_attempt_id
                else None,
            },
        )

    @staticmethod
    def _existing_item(
        uow: Any, invocation_id: UUID, item_key: str
    ) -> dict[str, Any] | None:
        with uow.connection.cursor() as cur:
            cur.execute(
                "SELECT output FROM research_invocations WHERE id=%s FOR UPDATE",
                (invocation_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise DirectScrapePersistenceError("direct scrape invocation not found")
        return DirectScrapeService._items_by_key(row[0]).get(item_key)

    @staticmethod
    def _items_by_key(output: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
        if not output:
            return {}
        items = output.get("items", [])
        if not isinstance(items, list):
            raise DirectScrapePersistenceError(
                "invalid direct scrape invocation output"
            )
        return {
            str(item["item_key"]): dict(item)
            for item in items
            if isinstance(item, Mapping) and item.get("item_key")
        }

    @staticmethod
    def _next_attempt_number(uow: Any, candidate_id: UUID) -> int:
        with uow.connection.cursor() as cur:
            cur.execute(
                "SELECT id FROM search_candidates WHERE id=%s FOR UPDATE",
                (candidate_id,),
            )
            if cur.fetchone() is None:
                raise DirectScrapePersistenceError(
                    f"direct scrape candidate disappeared: {candidate_id}"
                )
            cur.execute(
                (
                    "SELECT COALESCE(MAX(attempt_number),0)+1 "
                    "FROM extraction_attempts WHERE candidate_id=%s"
                ),
                (candidate_id,),
            )
            return int(cur.fetchone()[0])

    @staticmethod
    def _link_run_asset(
        uow: Any,
        run_id: UUID,
        snapshot_id: UUID,
        invocation_id: UUID,
        target: _ResolvedTarget,
    ) -> None:
        with uow.connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_run_assets(run_id,snapshot_id,role,metadata)
                VALUES(%s,%s,'acquired',%s)
                ON CONFLICT(run_id,snapshot_id,role) DO UPDATE
                  SET metadata=research_run_assets.metadata || excluded.metadata""",
                (
                    run_id,
                    snapshot_id,
                    json.dumps(
                        {
                            "invocation_id": str(invocation_id),
                            "candidate_id": str(target.candidate_id),
                            "item_key": target.item_key,
                        }
                    ),
                ),
            )

    @staticmethod
    def _method_for(request: DirectScrapeRequest) -> str:
        if request.schema is not None:
            return "structured_extraction"
        if request.effective_format == "markdown" and not request.summary:
            return "firecrawl_main_content"
        return "firecrawl_full_page"

    @staticmethod
    def _batch_status(items: Sequence[DirectScrapeItemResult]) -> str:
        succeeded = sum(item.status == "succeeded" for item in items)
        if succeeded == len(items):
            return "complete"
        if succeeded:
            return "partial"
        return "failed"

    @staticmethod
    def _require_context_run(context: AuthoritativeAcquisitionContext) -> UUID:
        if context.run_id is None or context.lifecycle_revision is None:
            raise DirectScrapePersistenceError(
                "direct scrape requires a run-bound authoritative context"
            )
        return context.run_id

    @staticmethod
    def _revalidate_run(
        uow: Any,
        context: AuthoritativeAcquisitionContext,
    ) -> tuple[str, int]:
        run_id = DirectScrapeService._require_context_run(context)
        with uow.connection.cursor() as cur:
            cur.execute(
                (
                    "SELECT state,lifecycle_revision FROM research_runs "
                    "WHERE id=%s FOR UPDATE"
                ),
                (run_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise DirectScrapePersistenceError(
                    f"research run disappeared: {run_id}"
                )
            state, revision = str(row[0]), int(row[1])
            if state not in ACQUISITION_ENTRY_STATES:
                raise DirectScrapePersistenceError(
                    f"research run is no longer acquisition-eligible: {state}"
                )
            if revision != context.lifecycle_revision:
                raise DirectScrapePersistenceError(
                    "research run lifecycle revision changed before persistence: "
                    f"expected {context.lifecycle_revision}, current {revision}"
                )
            return state, revision

    def _notify_jobs(self, chunk_ids: Sequence[UUID]) -> None:
        if self.queue is None:
            return
        for chunk_id in chunk_ids:
            try:
                self.queue.notify(chunk_id)
            except Exception:  # noqa: BLE001, S110
                pass

    @staticmethod
    def _diagnostic(value: bytes) -> str:
        return value.decode("utf-8", errors="replace")[:_MAX_DIAGNOSTIC_CHARS].strip()

    @staticmethod
    def _bounded_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(value, default=str, sort_keys=True)
        if len(encoded) <= _MAX_DIAGNOSTIC_CHARS:
            return dict(value)
        return {"truncated": encoded[:_MAX_DIAGNOSTIC_CHARS]}


def build_direct_scrape_service(
    config: StoreConfig | None = None,
    *,
    adapter_factory: Callable[[], DirectScrapeAdapter] | None = None,
) -> DirectScrapeService:
    """Compose direct-scrape policy with an explicit provider transport."""
    from functools import partial

    from ..blob import ContentAddressedBlobStore
    from ..corpus_service import CorpusService
    from ..parsing import get_registry
    from ..postgres import PostgresUnitOfWork

    if adapter_factory is None:
        from .adapters.firecrawl_scrape import FirecrawlDirectScrapeAdapter

        adapter_factory = FirecrawlDirectScrapeAdapter

    resolved = config or StoreConfig.from_env()
    resolved.require_database()
    uow_factory = partial(
        PostgresUnitOfWork,
        resolved.database_url,
        resolved.physical_collection,
        resolved.embedding_model,
        resolved.embedding_revision,
        resolved.embedding_dimension,
        resolved.parser_version,
        resolved.normalization_version,
        resolved.chunker_version,
    )
    blob_store = ContentAddressedBlobStore(resolved.blob_root)
    corpus_service = CorpusService(
        resolved,
        uow_factory,
        blob_store,
        parser_registry=get_registry(),
    )
    return DirectScrapeService(
        resolved,
        uow_factory,
        blob_store,
        corpus_service,
        adapter_factory=adapter_factory,
    )


__all__ = [
    "DIRECT_SCRAPE_TABLE_PRIVILEGES",
    "DirectScrapeError",
    "DirectScrapePersistenceError",
    "DirectScrapeService",
    "_ResolvedTarget",
    "build_direct_scrape_service",
    "require_direct_scrape_persistence",
]
