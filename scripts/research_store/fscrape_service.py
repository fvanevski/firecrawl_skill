"""PostgreSQL-authoritative direct scrape service used by ``fscrape``."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .config import StoreConfig
from .direct_scrape_service import (
    DirectScrapeBatchResult,
    DirectScrapePersistenceError,
    DirectScrapeRequest,
    DirectScrapeService,
    FirecrawlDirectScrapeAdapter,
    ScrapeTransportResult,
    build_direct_scrape_service,
)
from .fscrape_contract import (
    FScrapeError,
    FScrapeRequest,
    FScrapeResult,
    new_invocation_id,
)


class ValidatedDirectScrapeService(DirectScrapeService):
    """Validate structured provider output before authoritative ingestion."""

    def _persist_success(
        self,
        context: Any,
        invocation_id: UUID,
        target: Any,
        transport: ScrapeTransportResult,
    ) -> Any:
        schema = target.request.schema
        if schema is not None:
            try:
                payload = json.loads(transport.raw_payload.decode("utf-8"))
                Draft202012Validator(schema).validate(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
                failed = replace(
                    transport,
                    returncode=1,
                    stderr=f"schema validation failed: {exc}".encode(),
                    metadata={
                        **dict(transport.metadata),
                        "failure_class": "schema_validation",
                    },
                )
                return self._persist_failure(
                    context,
                    invocation_id,
                    target,
                    failed,
                )
        return super()._persist_success(context, invocation_id, target, transport)


class FScrapeService:
    """Run one authoritative direct-scrape batch and report stable identities."""

    def __init__(self, direct_service: DirectScrapeService, run_service: Any) -> None:
        self.direct_service = direct_service
        self.run_service = run_service

    def execute(self, request: FScrapeRequest) -> FScrapeResult:
        run_status = self._resolve_run(request.research_run_id)
        run_id = UUID(str(run_status.id))
        external_id = request.external_invocation_id or new_invocation_id()
        direct_requests = request.direct_requests()
        key = request.idempotency_key or default_idempotency_key(
            run_id,
            external_id,
            direct_requests,
        )
        batch = self.direct_service.execute(
            run_id,
            direct_requests,
            idempotency_key=key,
            external_invocation_id=external_id,
        )
        return FScrapeResult(
            research_run_id=request.research_run_id,
            external_invocation_id=external_id,
            batch=batch,
            index_job_ids_by_chunk=self._index_job_ids(batch),
        )

    def _resolve_run(self, research_run_id: str) -> Any:
        try:
            return self.run_service.status(external_id=research_run_id)
        except KeyError as exc:
            raise FScrapeError(
                "preflight", f"research run does not exist: {research_run_id}"
            ) from exc
        except Exception as exc:
            raise FScrapeError(
                "preflight", f"research run lookup failed: {exc}"
            ) from exc

    def _index_job_ids(
        self, batch: DirectScrapeBatchResult
    ) -> dict[UUID, tuple[UUID, ...]]:
        chunks = tuple(
            dict.fromkeys(
                UUID(str(chunk))
                for item in batch.items
                if item.status == "succeeded"
                for chunk in item.chunk_ids
            )
        )
        if not chunks:
            return {}
        with self.direct_service.uow_factory() as uow, uow.connection.cursor() as cur:
            cur.execute(
                """SELECT id,entity_id
                FROM index_jobs
                WHERE entity_type='chunk' AND entity_id=ANY(%s)
                ORDER BY entity_id,id""",
                (list(chunks),),
            )
            rows = cur.fetchall()
        values: dict[UUID, list[UUID]] = {}
        for job_id, chunk_id in rows:
            values.setdefault(UUID(str(chunk_id)), []).append(UUID(str(job_id)))
        missing = [chunk for chunk in chunks if chunk not in values]
        if missing:
            raise DirectScrapePersistenceError(
                "authoritative scrape committed chunks without index jobs: "
                + ", ".join(str(value) for value in missing[:10]),
                stage="indexing",
            )
        return {chunk: tuple(job_ids) for chunk, job_ids in values.items()}


def build_fscrape_service(
    config: StoreConfig | None = None,
    *,
    adapter_factory: Callable[[], Any] = FirecrawlDirectScrapeAdapter,
) -> FScrapeService:
    """Build the service without constructing Firecrawl before preflight."""
    from .container import build_run_service

    resolved = config or StoreConfig.from_env()
    resolved.require_database()
    base = build_direct_scrape_service(resolved, adapter_factory=adapter_factory)
    direct = ValidatedDirectScrapeService(
        base.config,
        base.uow_factory,
        base.blob_store,
        base.corpus_service,
        adapter_factory=base.adapter_factory,
        preflight=base.preflight,
        authority_check=base.authority_check,
        queue=base.queue,
    )
    return FScrapeService(direct, build_run_service(resolved))


def default_idempotency_key(
    run_id: UUID,
    external_invocation_id: str,
    requests: Sequence[DirectScrapeRequest],
) -> str:
    payload = json.dumps(
        {
            "run_id": str(run_id),
            "external_invocation_id": external_invocation_id,
            "requests": [
                {
                    "url": item.url,
                    "format": item.effective_format,
                    "summary": item.effective_summary,
                    "schema": item.schema,
                    "mime_type": item.effective_mime_type,
                }
                for item in requests
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"fscrape:{hashlib.sha256(payload.encode()).hexdigest()}"
