"""PostgreSQL-authoritative direct scrape service used by ``fscrape``."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .acquisition.direct_scrape_application import (
    DirectScrapePersistenceError,
    DirectScrapeService,
)
from .acquisition.models import (
    DirectScrapeBatchResult,
    DirectScrapeRequest,
    ScrapeTransportResult,
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
        requested_external_id = request.external_invocation_id or new_invocation_id()
        direct_requests = request.direct_requests()
        if request.idempotency_key is not None:
            key = request.idempotency_key
        elif request.fresh:
            key = fresh_idempotency_key(run_id, requested_external_id)
        else:
            key = default_idempotency_key(run_id, direct_requests)
        batch = self.direct_service.execute(
            run_id,
            direct_requests,
            idempotency_key=key,
            external_invocation_id=requested_external_id,
        )
        authoritative_external_id = self._authoritative_external_invocation_id(batch)
        return FScrapeResult(
            research_run_id=request.research_run_id,
            external_invocation_id=authoritative_external_id,
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

    def _authoritative_external_invocation_id(
        self, batch: DirectScrapeBatchResult
    ) -> str:
        with self.direct_service.uow_factory() as uow, uow.connection.cursor() as cur:
            cur.execute(
                """SELECT external_invocation_id
                FROM research_invocations
                WHERE id=%s AND run_id=%s AND operation='direct_scrape'""",
                (batch.invocation_id, batch.run_id),
            )
            row = cur.fetchone()
        if row is None:
            raise DirectScrapePersistenceError(
                "authoritative direct scrape invocation is not committed"
            )
        external_id = row[0]
        if not external_id:
            raise DirectScrapePersistenceError(
                "authoritative direct scrape invocation has no external identity"
            )
        return str(external_id)

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


def default_idempotency_key(
    run_id: UUID,
    requests: Sequence[DirectScrapeRequest],
) -> str:
    """Logical replay key: run plus normalized content-affecting semantics.

    The key deliberately excludes the auto-generated external invocation ID so
    that logically identical calls in the same run replay the authoritative
    batch instead of minting duplicate extraction work.
    """
    payload = json.dumps(
        {
            "run_id": str(run_id),
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


def fresh_idempotency_key(run_id: UUID, external_invocation_id: str) -> str:
    """Unique key for a genuinely fresh scrape that must not replay."""
    payload = json.dumps(
        {
            "run_id": str(run_id),
            "external_invocation_id": external_invocation_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"fscrape:fresh:{hashlib.sha256(payload.encode()).hexdigest()}"


__all__ = [
    "FScrapeService",
    "ValidatedDirectScrapeService",
    "default_idempotency_key",
    "fresh_idempotency_key",
]
