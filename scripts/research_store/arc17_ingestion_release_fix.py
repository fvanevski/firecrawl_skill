"""ARC-17 correction for extraction-backed ingestion batch finalization.

Issue #217 made ingestion batches derive timing and outcomes from exact extraction
attempts. The bounded extraction path, however, persisted and linked a successful
snapshot before the corresponding extraction attempt had a terminal end time.
The asset-promotion guard can therefore reject a valid run-asset link and make a
successful corpus member appear failed.

This module is installed after the issue #217 compatibility extension. It keeps
successful extraction completion, batch-member persistence, and run-asset
linkage in one PostgreSQL transaction. The only idempotent completion shim is
scoped to the bounded-wave execution that deliberately replays the successful
completion after corpus ingestion; ordinary ExtractionService completion remains
unchanged.
"""

from __future__ import annotations

from io import BytesIO
from types import MethodType
from typing import Any
from uuid import UUID

_ORIGINAL_FINALIZE_BATCH = None
_ORIGINAL_BOUNDED_EXECUTE = None


def _same_blob(existing: Any, supplied: Any) -> bool:
    if supplied is None:
        return True
    if existing is None:
        return False
    return (
        existing.sha256 == supplied.sha256
        and existing.byte_length == supplied.byte_length
        and existing.uri == supplied.uri
    )


def _corpus_ingest_batch(
    self,
    invocation_id: str,
    operation: str,
    requests: list,
    *,
    research_run_external_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Persist extraction-backed success and run linkage atomically."""
    from .domain import IngestRequest, utcnow

    failures = 0
    with self.uow_factory() as uow:
        batch_id = uow.start_ingestion_batch(
            invocation_id, operation, research_run_external_id, metadata
        )
        seen_ordinals: set[int] = set()
        for fallback_ordinal, item in enumerate(requests):
            ordinal = fallback_ordinal
            item_mapping = item if isinstance(item, dict) else None
            if item_mapping is not None:
                result_index = (
                    item_mapping.get("metadata", {})
                    .get("firecrawl", {})
                    .get("result_index")
                )
                if isinstance(result_index, int) and result_index >= 0:
                    ordinal = result_index
            if ordinal in seen_ordinals:
                raise ValueError(f"duplicate ingestion result ordinal: {ordinal}")
            seen_ordinals.add(ordinal)

            request = item if isinstance(item, IngestRequest) else item.get("request")
            requested_url = (
                request.requested_url
                if request is not None
                else item.get("requested_url") or item.get("url") or "unknown:"
            )
            item_metadata = item.get("metadata") if isinstance(item, dict) else None
            explicit_attempt_id = (
                item.get("extraction_attempt_id") if isinstance(item, dict) else None
            )
            attempt_id = (
                request.extraction_attempt_id
                if request is not None and request.extraction_attempt_id is not None
                else explicit_attempt_id
            )
            constituent_started_at = utcnow()
            try:
                if request is None:
                    raise RuntimeError(item.get("error") or "acquisition failed")
                prepared = self._prepare_ingest(request)
                with uow.savepoint():
                    result = uow.persist_ingest(*prepared.persist_args())
                    constituent_completed_at = utcnow()

                    # Extraction-backed success is terminalized in the same
                    # transaction as the authoritative snapshot/member/link.
                    # This satisfies the promotion guard without creating a
                    # window in which a retained run asset cites a nonterminal
                    # attempt. Content-addressed duplicate blob writes are safe.
                    if attempt_id is not None:
                        raw_blob = self.blob_store.put(BytesIO(request.content), None)
                        normalized_blob = self.blob_store.put(
                            BytesIO(request.normalized_content or request.content), None
                        )
                        status_code = None
                        if isinstance(item_metadata, dict):
                            firecrawl = item_metadata.get("firecrawl")
                            if isinstance(firecrawl, dict):
                                status_code = firecrawl.get("status_code")
                        if status_code is None:
                            status_code = request.http_status
                        uow.extraction_attempts.complete_attempt(
                            attempt_id=attempt_id,
                            exit_status="succeeded",
                            raw_blob=raw_blob,
                            normalized_blob=normalized_blob,
                            parser_used=self.config.parser_version,
                            quality_metrics=None,
                            failure_class="none",
                            http_status=status_code,
                            backend_status="complete",
                            end_time=constituent_completed_at,
                            error_message=None,
                        )

                    uow.record_batch_asset(
                        batch_id,
                        ordinal,
                        requested_url,
                        "complete",
                        result,
                        metadata=item_metadata,
                        extraction_attempt_id=attempt_id,
                        constituent_started_at=constituent_started_at,
                        constituent_completed_at=constituent_completed_at,
                    )
                    if research_run_external_id:
                        uow.link_run_asset(
                            research_run_external_id,
                            result.snapshot_id,
                            "acquired",
                        )
            except Exception as exc:  # noqa: BLE001
                failures += 1
                constituent_completed_at = utcnow()
                uow.record_batch_asset(
                    batch_id,
                    ordinal,
                    requested_url,
                    "failed",
                    error=f"{type(exc).__name__}: {exc}",
                    metadata=item_metadata,
                    extraction_attempt_id=attempt_id,
                    constituent_started_at=constituent_started_at,
                    constituent_completed_at=constituent_completed_at,
                )
        manifest = uow.export_invocation(invocation_id)
    manifest["failure_count"] = failures
    return manifest


def _finalize_batch_with_canonical_identity(
    self, batch_id: str, status: str, error: str | None = None
) -> dict:
    """Keep finalization batch identity consistent with canonical DB exports."""
    manifest = _ORIGINAL_FINALIZE_BATCH(self, batch_id, status, error=error)
    if manifest.get("batch_id") is not None:
        manifest["batch_id"] = UUID(str(manifest["batch_id"]))
    return manifest


def _bounded_execute_with_scoped_success_replay(self, *args, **kwargs):
    """Allow only the bounded wave's deliberate second success completion."""
    extraction_service = self.extraction_service
    if extraction_service is None:
        return _ORIGINAL_BOUNDED_EXECUTE(self, *args, **kwargs)

    original_complete = extraction_service.complete_attempt

    def scoped_complete(
        service,
        attempt_id: UUID,
        exit_status: str,
        raw_blob=None,
        normalized_blob=None,
        parser_used: str | None = None,
        quality_metrics=None,
        failure_class: str = "none",
        http_status: int | None = None,
        backend_status: str | None = None,
        end_time=None,
        error_message: str | None = None,
    ):
        if exit_status == "succeeded":
            existing = service.get_attempt(attempt_id)
            if existing is not None and existing.end_time is not None:
                same = (
                    existing.exit_status == "succeeded"
                    and _same_blob(existing.raw_blob, raw_blob)
                    and _same_blob(existing.normalized_blob, normalized_blob)
                    and (parser_used is None or existing.parser_used == parser_used)
                    and existing.failure_class == failure_class
                    and (http_status is None or existing.http_status == http_status)
                    and (
                        backend_status is None
                        or existing.backend_status == backend_status
                    )
                    and (
                        error_message is None
                        or existing.error_message == error_message
                    )
                )
                if same:
                    return existing
                raise RuntimeError(
                    f"extraction attempt {attempt_id} is already finalized with "
                    "different authoritative evidence"
                )
        return original_complete(
            attempt_id=attempt_id,
            exit_status=exit_status,
            raw_blob=raw_blob,
            normalized_blob=normalized_blob,
            parser_used=parser_used,
            quality_metrics=quality_metrics,
            failure_class=failure_class,
            http_status=http_status,
            backend_status=backend_status,
            end_time=end_time,
            error_message=error_message,
        )

    extraction_service.complete_attempt = MethodType(scoped_complete, extraction_service)
    try:
        return _ORIGINAL_BOUNDED_EXECUTE(self, *args, **kwargs)
    finally:
        extraction_service.complete_attempt = original_complete


def install_arc17_ingestion_release_fix(service_module, bounded_module) -> None:
    """Install the ARC-17 ordering correction on the canonical bounded path."""
    global _ORIGINAL_FINALIZE_BATCH, _ORIGINAL_BOUNDED_EXECUTE

    if _ORIGINAL_FINALIZE_BATCH is None:
        _ORIGINAL_FINALIZE_BATCH = service_module.CorpusService.finalize_ingestion_batch
    if _ORIGINAL_BOUNDED_EXECUTE is None:
        _ORIGINAL_BOUNDED_EXECUTE = bounded_module.BoundedExtractionStage.execute

    service_module.CorpusService.ingest_batch = _corpus_ingest_batch
    service_module.CorpusService.finalize_ingestion_batch = (
        _finalize_batch_with_canonical_identity
    )
    bounded_module.BoundedExtractionStage.execute = (
        _bounded_execute_with_scoped_success_replay
    )
