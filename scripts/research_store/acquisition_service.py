from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from .acquisition_authority import (
    ACQUISITION_ENTRY_STATES,
    AuthoritativeAcquisitionContext,
)
from .blob import ContentAddressedBlobStore
from .domain import SearchAdapterResult, utcnow
from .ports import SearchAdapter


class FirecrawlSearchAdapter:
    """Wraps Firecrawl CLI or runner to execute search queries and classify transport errors."""

    def __init__(self, runner: Callable[..., tuple[int, bytes, str]] | None = None):
        self.runner = runner or self._default_runner

    @staticmethod
    def _default_runner(cmd: list[str], timeout: int = 60) -> tuple[int, bytes, str]:
        try:
            proc = subprocess.run(  # noqa: PLW1510
                cmd,
                capture_output=True,
                timeout=timeout,
            )
            return (
                proc.returncode,
                proc.stdout,
                proc.stderr.decode("utf-8", errors="replace"),
            )
        except subprocess.TimeoutExpired:
            return -1, b"", "ETIMEDOUT: Firecrawl search process timed out"
        except Exception as exc:  # noqa: BLE001
            return -1, b"", f"Transport error: {type(exc).__name__}: {exc}"

    def search(
        self,
        query_text: str,
        *,
        backend: str = "firecrawl",
        limit: int = 20,
        sources: str = "web",
        tbs: str | None = None,
        retries: int = 2,
        **kwargs: Any,
    ) -> SearchAdapterResult:
        if not query_text.strip():
            raise ValueError("query_text must be non-empty")

        if backend == "firecrawl_scrape":
            return self._scrape_url(query_text, retries=retries)

        cmd = [
            "firecrawl",
            "search",
            query_text,
            "--limit",
            str(limit),
            "--sources",
            sources,
            "--ignore-invalid-urls",
            "--scrape",
            "--scrape-formats",
            "markdown",
            "--json",
        ]
        if tbs:
            cmd.extend(["--tbs", tbs])

        requested_at = utcnow()
        attempt = 0
        last_stderr = ""
        last_code = 0
        stdout_data = b""

        while attempt <= retries:
            code, stdout, stderr = self.runner(cmd)
            responded_at = utcnow()
            last_code = code
            last_stderr = stderr
            stdout_data = stdout

            if code == 0 and stdout:
                return SearchAdapterResult(
                    raw_payload=stdout,
                    http_status=200,
                    provider_request_id=None,
                    transport_error=None,
                    transport_metadata={
                        "attempt": attempt + 1,
                        "cmd": cmd,
                        "exit_code": code,
                    },
                    requested_at=requested_at,
                    responded_at=responded_at,
                )

            is_transient = any(
                err_code in stderr
                for err_code in ("EAI_AGAIN", "ENOTFOUND", "ECONNRESET", "ETIMEDOUT")
            )
            if not is_transient or attempt >= retries:
                break
            attempt += 1

        transport_err = None
        if last_code != 0 or not stdout_data:
            for err_tag in ("EAI_AGAIN", "ENOTFOUND", "ECONNRESET", "ETIMEDOUT"):
                if err_tag in last_stderr:
                    transport_err = f"Network transport error: {err_tag}"
                    break
            if not transport_err:
                if last_stderr.strip():
                    transport_err = f"Firecrawl search failed (exit {last_code}): {last_stderr.strip()[:300]}"
                else:
                    transport_err = (
                        f"Firecrawl search failed with exit code {last_code}"
                    )

        payload = (
            stdout_data
            if (stdout_data and last_code == 0)
            else json.dumps(
                {
                    "success": False,
                    "error": transport_err or "Empty response from search provider",
                }
            ).encode("utf-8")
        )

        return SearchAdapterResult(
            raw_payload=payload,
            http_status=500 if transport_err else None,
            provider_request_id=None,
            transport_error=transport_err,
            transport_metadata={
                "attempts": attempt + 1,
                "cmd": cmd,
                "exit_code": last_code,
                "stderr": last_stderr[:500],
            },
            requested_at=requested_at,
            responded_at=responded_at,
        )

    def _scrape_url(self, url: str, *, retries: int) -> SearchAdapterResult:
        """Scrape one labeled benchmark source through the real CLI."""
        cmd = [
            "firecrawl",
            "scrape",
            url,
            "--format",
            "markdown",
            "--only-main-content",
            "--json",
        ]
        requested_at = utcnow()
        last_code = 0
        last_stderr = ""
        for attempt in range(retries + 1):
            code, stdout, stderr = self.runner(cmd)
            responded_at = utcnow()
            last_code = code
            last_stderr = stderr
            if code == 0 and stdout:
                try:
                    scraped = json.loads(stdout)
                    markdown = scraped.get("markdown")
                    metadata = scraped.get("metadata") or {}
                    if not isinstance(markdown, str) or not markdown.strip():
                        raise ValueError("scrape response has no markdown")
                    payload = {
                        "success": True,
                        "data": {
                            "web": [
                                {
                                    "url": metadata.get("url") or url,
                                    "title": url.rsplit("/", 1)[-1],
                                    "description": "versioned benchmark source",
                                    "markdown": markdown,
                                    "metadata": metadata,
                                }
                            ]
                        },
                    }
                except (json.JSONDecodeError, ValueError) as exc:
                    last_stderr = f"invalid Firecrawl scrape response: {exc}"
                else:
                    return SearchAdapterResult(
                        raw_payload=json.dumps(payload).encode("utf-8"),
                        http_status=200,
                        provider_request_id=metadata.get("scrapeId"),
                        transport_error=None,
                        transport_metadata={
                            "attempt": attempt + 1,
                            "cmd": cmd,
                            "exit_code": code,
                            "operation": "scrape",
                        },
                        requested_at=requested_at,
                        responded_at=responded_at,
                    )
            if not any(
                tag in stderr
                for tag in ("EAI_AGAIN", "ENOTFOUND", "ECONNRESET", "ETIMEDOUT")
            ):
                break
        error = (
            f"Firecrawl scrape failed (exit {last_code}): {last_stderr.strip()[:300]}"
        )
        return SearchAdapterResult(
            raw_payload=json.dumps({"success": False, "error": error}).encode(),
            http_status=500,
            provider_request_id=None,
            transport_error=error,
            transport_metadata={"cmd": cmd, "exit_code": last_code},
            requested_at=requested_at,
            responded_at=utcnow(),
        )


class AcquisitionAuthorityChangedError(RuntimeError):
    """The preflight authority snapshot became stale before commit."""


class AcquisitionIdempotencyConflictError(RuntimeError):
    """An idempotency key was reused for a different search request."""


@dataclass(frozen=True)
class AcquisitionResult:
    search_response_id: UUID
    run_id: UUID
    query_text: str
    backend: str
    status: str
    candidate_count: int
    candidates: list[dict[str, Any]]
    postgres_committed: bool
    event_id: UUID | None = None
    search_response: dict[str, Any] = field(default_factory=dict)
    replayed: bool = False


class AcquisitionService:
    """Service boundary for executing search acquisition and persisting results transactionally."""

    def __init__(
        self,
        uow_factory: Callable,
        blob_store: Any | None = None,
        search_adapter: SearchAdapter | None = None,
    ):
        self.uow_factory = uow_factory
        self.blob_store = blob_store
        self.search_adapter = search_adapter or FirecrawlSearchAdapter()

    def execute_search(
        self,
        run_id: UUID,
        query_text: str,
        *,
        backend: str = "firecrawl",
        plan_id: UUID | None = None,
        plan_query_id: UUID | None = None,
        idempotency_key: str | None = None,
        limit: int = 20,
        sources: str = "web",
        tbs: str | None = None,
        metadata: dict[str, Any] | None = None,
        authority_context: AuthoritativeAcquisitionContext | None = None,
        replay_existing: bool = False,
    ) -> AcquisitionResult:
        run_id = UUID(str(run_id))
        if plan_id is not None:
            plan_id = UUID(str(plan_id))
        if plan_query_id is not None:
            plan_query_id = UUID(str(plan_query_id))
        if not query_text.strip():
            raise ValueError("query_text must be non-empty")

        key = idempotency_key or f"search:{run_id}:{plan_query_id or query_text}"
        request_envelope = {
            "schema_version": "authoritative-search-request-v1",
            "query_text": query_text,
            "backend": backend,
            "limit": int(limit),
            "sources": sources,
            "tbs": tbs,
            "parser_version": "firecrawl-search-v1",
        }
        store = self.blob_store
        if store is None:
            store = ContentAddressedBlobStore(
                Path(os.environ.get("BLOB_ROOT", "data/blobs"))
            )

        lock = (
            self._search_idempotency_lock(run_id, key)
            if replay_existing
            else nullcontext(None)
        )
        with lock as lock_uow:
            if lock_uow is not None:
                existing = self._load_existing_search(
                    lock_uow,
                    run_id,
                    key,
                    request_envelope,
                    authority_context,
                )
                if existing is not None:
                    return existing

            adapter_result = self.search_adapter.search(
                query_text,
                backend=backend,
                limit=limit,
                sources=sources,
                tbs=tbs,
            )

            postgres_committed = False
            event_id = None
            candidates: list[dict[str, Any]] = []
            resp_data: dict[str, Any] = {}
            with self.uow_factory() as uow:
                self._revalidate_authority(uow, authority_context, run_id)
                persisted_metadata = dict(metadata or {})
                persisted_metadata["request_envelope"] = request_envelope
                resp_data = uow.runs.record_search_response(
                    run_id,
                    query_text,
                    backend,
                    adapter_result.raw_payload,
                    key,
                    store,
                    plan_id=plan_id,
                    plan_query_id=plan_query_id,
                    provider_request_id=adapter_result.provider_request_id,
                    http_status=adapter_result.http_status,
                    error_message=adapter_result.transport_error,
                    requested_at=adapter_result.requested_at,
                    responded_at=adapter_result.responded_at,
                    transport_metadata=adapter_result.transport_metadata,
                    **persisted_metadata,
                )
                resp_id = resp_data["id"]
                if resp_data["status"] in ("succeeded", "empty"):
                    candidates = uow.runs.record_response_candidates(
                        run_id,
                        resp_id,
                        store,
                        plan_id=plan_id,
                        plan_query_id=plan_query_id,
                    )
                event_id = uow.runs.append_event(
                    run_id,
                    "acquisition.search_executed",
                    "system",
                    f"event:{key}",
                    payload={
                        "search_response_id": str(resp_id),
                        "query_text": query_text,
                        "backend": backend,
                        "status": resp_data["status"],
                        "candidate_count": len(candidates),
                        "idempotency_key": key,
                    },
                )
                uow.commit()
                postgres_committed = True

        return AcquisitionResult(
            search_response_id=resp_data["id"],
            run_id=run_id,
            query_text=query_text,
            backend=backend,
            status=resp_data["status"],
            candidate_count=len(candidates),
            candidates=candidates,
            postgres_committed=postgres_committed,
            event_id=event_id,
            search_response=resp_data,
            replayed=False,
        )

    @contextmanager
    def _search_idempotency_lock(
        self,
        run_id: UUID,
        idempotency_key: str,
    ) -> Iterator[Any]:
        lock_name = f"authoritative-search:{run_id}:{idempotency_key}"
        with self.uow_factory() as uow:
            with uow.connection.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s,0))",
                    (lock_name,),
                )
            uow.connection.commit()
            try:
                yield uow
            finally:
                uow.connection.rollback()
                with uow.connection.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                        (lock_name,),
                    )
                uow.connection.commit()

    def _load_existing_search(
        self,
        uow: Any,
        run_id: UUID,
        idempotency_key: str,
        request_envelope: Mapping[str, Any],
        authority_context: AuthoritativeAcquisitionContext | None,
    ) -> AcquisitionResult | None:
        with uow.connection.cursor() as cur:
            cur.execute(
                """SELECT id,query_text,backend,status,error_message,
                          transport_metadata,provider_request_id,http_status,
                          parser_version,raw_blob_sha256,raw_blob_bytes,mime_type,
                          content_sha256,result_count,payload_summary,
                          requested_at,responded_at,created_at
                   FROM search_responses
                   WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            row = cur.fetchone()
            if row is None:
                return None
            self._revalidate_authority(uow, authority_context, run_id)
            stored_transport = row[5] or {}
            stored_envelope = stored_transport.get("request_envelope")
            if stored_envelope != dict(request_envelope):
                raise AcquisitionIdempotencyConflictError(
                    "search idempotency key was used for another request"
                )
            cur.execute(
                """SELECT o.id,o.candidate_id,o.rank,c.canonical_url,
                          c.original_url,o.title,o.snippet,o.raw_item
                   FROM candidate_occurrences o
                   JOIN search_candidates c ON c.id=o.candidate_id
                   WHERE o.run_id=%s AND o.search_response_id=%s
                   ORDER BY o.rank,o.id""",
                (run_id, row[0]),
            )
            candidates = [
                {
                    "id": item[0],
                    "candidate_id": item[1],
                    "rank": item[2],
                    "canonical_url": item[3],
                    "original_url": item[4],
                    "title": item[5],
                    "snippet": item[6],
                    "raw_item": item[7],
                }
                for item in cur.fetchall()
            ]
        response = {
            "id": row[0],
            "run_id": run_id,
            "query_text": row[1],
            "backend": row[2],
            "status": row[3],
            "error_message": row[4],
            "transport_metadata": stored_transport,
            "provider_request_id": row[6],
            "http_status": row[7],
            "parser_version": row[8],
            "raw_blob_sha256": row[9],
            "raw_blob_bytes": row[10],
            "mime_type": row[11],
            "content_sha256": row[12],
            "result_count": row[13],
            "payload_summary": row[14],
            "idempotency_key": idempotency_key,
            "requested_at": row[15],
            "responded_at": row[16],
            "created_at": row[17],
        }
        return AcquisitionResult(
            search_response_id=row[0],
            run_id=run_id,
            query_text=row[1],
            backend=row[2],
            status=row[3],
            candidate_count=len(candidates),
            candidates=candidates,
            postgres_committed=True,
            search_response=response,
            replayed=True,
        )

    @staticmethod
    def _revalidate_authority(
        uow: Any,
        context: AuthoritativeAcquisitionContext | None,
        run_id: UUID,
    ) -> None:
        if context is None:
            return
        if context.run_id != run_id or context.lifecycle_revision is None:
            raise AcquisitionAuthorityChangedError(
                "authoritative search requires the matching run-bound preflight context"
            )
        with uow.connection.cursor() as cur:
            cur.execute(
                "SELECT state,lifecycle_revision FROM research_runs WHERE id=%s FOR UPDATE",
                (run_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise AcquisitionAuthorityChangedError(
                f"research run disappeared before search persistence: {run_id}"
            )
        state, revision = str(row[0]), int(row[1])
        if state not in ACQUISITION_ENTRY_STATES:
            raise AcquisitionAuthorityChangedError(
                f"research run is no longer acquisition-eligible: {state}"
            )
        if revision != context.lifecycle_revision:
            raise AcquisitionAuthorityChangedError(
                "research run lifecycle revision changed before search persistence: "
                f"expected {context.lifecycle_revision}, current {revision}"
            )

    def reconcile_pending_searches(self, run_id: UUID) -> list[dict[str, Any]]:
        """Reconcile search responses for a run to ensure candidates are extracted without duplicates."""
        run_id = UUID(str(run_id))
        reconciled = []
        store = self.blob_store
        if store is None:
            store = ContentAddressedBlobStore(
                Path(os.environ.get("BLOB_ROOT", "data/blobs"))
            )

        with self.uow_factory() as uow:
            responses = uow.runs.list_search_responses(run_id)
            for resp in responses:
                if resp["status"] in ("succeeded", "empty"):
                    cands = uow.runs.record_response_candidates(
                        run_id,
                        resp["id"],
                        store,
                        plan_id=resp.get("plan_id"),
                        plan_query_id=resp.get("plan_query_id"),
                    )
                    reconciled.append(
                        {
                            "search_response_id": resp["id"],
                            "candidate_count": len(cands),
                            "status": resp["status"],
                        }
                    )
            uow.commit()
        return reconciled
