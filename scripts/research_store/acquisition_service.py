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
    AcquisitionPreflightError,
    AuthoritativeAcquisitionContext,
)
from .blob import ContentAddressedBlobStore
from .domain import SearchAdapterResult, utcnow
from .ports import SearchAdapter


class FirecrawlSearchAdapter:
    """Execute Firecrawl search queries and classify transport errors."""

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
                    transport_err = (
                        f"Firecrawl search failed (exit {last_code}): "
                        f"{last_stderr.strip()[:300]}"
                    )
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
            transport_metadata={
                "attempt": attempt + 1,
                "attempts": attempt + 1,
                "cmd": cmd,
                "exit_code": last_code,
            },
            requested_at=requested_at,
            responded_at=utcnow(),
        )


class AcquisitionAuthorityChangedError(RuntimeError):
    """The preflight authority snapshot became stale before commit."""


class AcquisitionIdempotencyConflictError(RuntimeError):
    """An idempotency key was reused for a different search request."""


class SearchProvenanceError(RuntimeError):
    """Search provenance could not be established without guessing."""


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
    invocation_id: UUID | None = None
    attempt_ordinal: int | None = None


class AcquisitionService:
    """Execute provider searches with PostgreSQL-authoritative relational provenance."""

    def __init__(
        self,
        uow_factory: Callable,
        blob_store: Any | None = None,
        search_adapter: SearchAdapter | None = None,
        *,
        config: Any | None = None,
        authority_preflight: Callable[..., AuthoritativeAcquisitionContext]
        | None = None,
    ):
        self.uow_factory = uow_factory
        self.blob_store = blob_store
        self.search_adapter = search_adapter or FirecrawlSearchAdapter()
        self.config = config
        self.authority_preflight = authority_preflight

    def execute_search(
        self,
        run_id: UUID,
        query_text: str,
        *,
        backend: str = "firecrawl",
        plan_id: UUID | None = None,
        plan_query_id: UUID | None = None,
        parent_invocation_id: UUID | None = None,
        idempotency_key: str | None = None,
        limit: int = 20,
        sources: str = "web",
        tbs: str | None = None,
        metadata: dict[str, Any] | None = None,
        authority_context: AuthoritativeAcquisitionContext | None = None,
        replay_existing: bool = True,
    ) -> AcquisitionResult:
        run_id = UUID(str(run_id))
        plan_id = UUID(str(plan_id)) if plan_id is not None else None
        plan_query_id = UUID(str(plan_query_id)) if plan_query_id is not None else None
        if (plan_id is None) != (plan_query_id is None):
            raise SearchProvenanceError(
                "planned search provenance requires both plan_id and plan_query_id"
            )
        if not query_text.strip():
            raise ValueError("query_text must be non-empty")

        inherited_parent = (metadata or {}).get("invocation_id")
        if parent_invocation_id is None and inherited_parent:
            parent_invocation_id = UUID(str(inherited_parent))
        elif parent_invocation_id is not None:
            parent_invocation_id = UUID(str(parent_invocation_id))

        authority_context = self._resolve_authority_context(run_id, authority_context)
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

            provider_invocation_id = self._begin_provider_attempt(
                run_id,
                query_text,
                backend,
                key,
                request_envelope,
                authority_context,
                plan_id=plan_id,
                plan_query_id=plan_query_id,
                parent_invocation_id=parent_invocation_id,
            )

            try:
                adapter_result = self.search_adapter.search(
                    query_text,
                    backend=backend,
                    limit=limit,
                    sources=sources,
                    tbs=tbs,
                )
            except BaseException as exc:
                self._terminalize_without_response(
                    run_id,
                    provider_invocation_id,
                    plan_id=plan_id,
                    plan_query_id=plan_query_id,
                    cancelled=isinstance(exc, (KeyboardInterrupt, SystemExit)),
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise

            attempt_ordinal = self._provider_attempt_ordinal(adapter_result)
            cancelled = bool(
                (adapter_result.transport_metadata or {}).get("cancelled", False)
            )

            postgres_committed = False
            event_id = None
            candidates: list[dict[str, Any]] = []
            resp_data: dict[str, Any] = {}
            with self.uow_factory() as uow:
                self._revalidate_authority(uow, authority_context, run_id)
                persisted_metadata = dict(metadata or {})
                persisted_metadata.pop("invocation_id", None)
                persisted_metadata["request_envelope"] = request_envelope
                persisted_metadata["provider_invocation_id"] = str(
                    provider_invocation_id
                )
                persisted_metadata["attempt_ordinal"] = attempt_ordinal

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
                resp_id = UUID(str(resp_data["id"]))
                terminal_state = self._query_terminal_state(
                    str(resp_data["status"]), cancelled=cancelled
                )

                with uow.connection.cursor() as cur:
                    cur.execute(
                        """UPDATE search_responses
                           SET invocation_id=%s,
                               attempt_ordinal=%s,
                               provenance_status='resolved'
                           WHERE id=%s AND run_id=%s
                           RETURNING invocation_id,attempt_ordinal,provenance_status""",
                        (
                            provider_invocation_id,
                            attempt_ordinal,
                            resp_id,
                            run_id,
                        ),
                    )
                    provenance_row = cur.fetchone()
                    if provenance_row is None or provenance_row[2] != "resolved":
                        raise SearchProvenanceError(
                            "search response provenance update did not complete"
                        )

                    if plan_query_id is not None:
                        cur.execute(
                            """UPDATE search_plan_queries
                               SET status=%s
                               WHERE id=%s AND plan_id=%s AND run_id=%s
                                 AND status='running'
                               RETURNING id""",
                            (
                                terminal_state,
                                plan_query_id,
                                plan_id,
                                run_id,
                            ),
                        )
                        if cur.fetchone() is None:
                            raise SearchProvenanceError(
                                "planned query was not running at response commit"
                            )

                    invocation_status = (
                        "cancelled"
                        if terminal_state == "cancelled"
                        else "complete"
                        if terminal_state in {"succeeded", "empty"}
                        else "failed"
                    )
                    cur.execute(
                        """UPDATE research_invocations
                           SET status=%s,
                               output=%s::jsonb,
                               error=%s,
                               completed_at=now()
                           WHERE id=%s AND run_id=%s AND status='running'
                           RETURNING id""",
                        (
                            invocation_status,
                            json.dumps(
                                {
                                    "search_response_id": str(resp_id),
                                    "status": str(resp_data["status"]),
                                    "attempt_ordinal": attempt_ordinal,
                                    "plan_id": str(plan_id) if plan_id else None,
                                    "plan_query_id": (
                                        str(plan_query_id) if plan_query_id else None
                                    ),
                                }
                            ),
                            adapter_result.transport_error,
                            provider_invocation_id,
                            run_id,
                        ),
                    )
                    if cur.fetchone() is None:
                        raise SearchProvenanceError(
                            "provider invocation was not running at response commit"
                        )

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
                    invocation_id=provider_invocation_id,
                    payload={
                        "search_response_id": str(resp_id),
                        "query_text": query_text,
                        "backend": backend,
                        "status": resp_data["status"],
                        "query_status": terminal_state,
                        "candidate_count": len(candidates),
                        "idempotency_key": key,
                        "attempt_ordinal": attempt_ordinal,
                        "plan_id": str(plan_id) if plan_id else None,
                        "plan_query_id": (
                            str(plan_query_id) if plan_query_id else None
                        ),
                    },
                )
                uow.commit()
                postgres_committed = True

        response_with_provenance = {
            **resp_data,
            "invocation_id": provider_invocation_id,
            "attempt_ordinal": attempt_ordinal,
            "provenance_status": "resolved",
        }
        return AcquisitionResult(
            search_response_id=UUID(str(resp_data["id"])),
            run_id=run_id,
            query_text=query_text,
            backend=backend,
            status=str(resp_data["status"]),
            candidate_count=len(candidates),
            candidates=candidates,
            postgres_committed=postgres_committed,
            event_id=event_id,
            search_response=response_with_provenance,
            replayed=False,
            invocation_id=provider_invocation_id,
            attempt_ordinal=attempt_ordinal,
        )

    def _begin_provider_attempt(
        self,
        run_id: UUID,
        query_text: str,
        backend: str,
        key: str,
        request_envelope: Mapping[str, Any],
        authority_context: AuthoritativeAcquisitionContext,
        *,
        plan_id: UUID | None,
        plan_query_id: UUID | None,
        parent_invocation_id: UUID | None,
    ) -> UUID:
        with self.uow_factory() as uow:
            self._revalidate_authority(uow, authority_context, run_id)
            with uow.connection.cursor() as cur:
                if parent_invocation_id is not None:
                    cur.execute(
                        "SELECT run_id FROM research_invocations WHERE id=%s",
                        (parent_invocation_id,),
                    )
                    parent = cur.fetchone()
                    if parent is None or UUID(str(parent[0])) != run_id:
                        raise SearchProvenanceError(
                            "parent invocation does not belong to the search run"
                        )

                if plan_query_id is not None:
                    cur.execute(
                        """SELECT query_text,status
                           FROM search_plan_queries
                           WHERE id=%s AND plan_id=%s AND run_id=%s
                           FOR UPDATE""",
                        (plan_query_id, plan_id, run_id),
                    )
                    plan_query = cur.fetchone()
                    if plan_query is None:
                        raise SearchProvenanceError(
                            "planned search query does not belong to the requested "
                            "plan/run"
                        )
                    if str(plan_query[0]) != query_text:
                        raise SearchProvenanceError(
                            "planned search query text does not match the persisted "
                            "plan row"
                        )
                    if plan_query[1] == "pending":
                        cur.execute(
                            """UPDATE search_plan_queries
                               SET status='running'
                               WHERE id=%s AND plan_id=%s AND run_id=%s
                                 AND status='pending'""",
                            (plan_query_id, plan_id, run_id),
                        )
                    elif plan_query[1] != "running":
                        raise SearchProvenanceError(
                            f"planned query is already terminal: {plan_query[1]}"
                        )

            provider_invocation_id = uow.runs.record_invocation(
                run_id,
                "search_provider",
                f"provider-search:{key}",
                parent_invocation_id=parent_invocation_id,
                status="running",
                input_payload=dict(request_envelope),
                metadata={
                    "backend": backend,
                    "plan_id": str(plan_id) if plan_id else None,
                    "plan_query_id": str(plan_query_id) if plan_query_id else None,
                    "query_text": query_text,
                },
            )
            with uow.connection.cursor() as cur:
                cur.execute(
                    """UPDATE research_invocations
                       SET started_at=COALESCE(started_at,now())
                       WHERE id=%s AND run_id=%s""",
                    (provider_invocation_id, run_id),
                )
            uow.commit()
        return UUID(str(provider_invocation_id))

    def _terminalize_without_response(
        self,
        run_id: UUID,
        invocation_id: UUID,
        *,
        plan_id: UUID | None,
        plan_query_id: UUID | None,
        cancelled: bool,
        error: str,
    ) -> None:
        terminal_state = "cancelled" if cancelled else "failed"
        with self.uow_factory() as uow:
            with uow.connection.cursor() as cur:
                if plan_query_id is not None:
                    cur.execute(
                        """UPDATE search_plan_queries
                           SET status=%s
                           WHERE id=%s AND plan_id=%s AND run_id=%s
                             AND status='running'""",
                        (terminal_state, plan_query_id, plan_id, run_id),
                    )
                cur.execute(
                    """UPDATE research_invocations
                       SET status=%s,error=%s,completed_at=now()
                       WHERE id=%s AND run_id=%s AND status='running'""",
                    (terminal_state, error[:1000], invocation_id, run_id),
                )
            uow.commit()

    @staticmethod
    def _provider_attempt_ordinal(adapter_result: SearchAdapterResult) -> int:
        metadata = adapter_result.transport_metadata or {}
        for field_name in ("attempt", "attempts"):
            raw = metadata.get(field_name)
            if isinstance(raw, bool):
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return 1

    @staticmethod
    def _query_terminal_state(status: str, *, cancelled: bool) -> str:
        if cancelled:
            return "cancelled"
        if status == "succeeded":
            return "succeeded"
        if status == "empty":
            return "empty"
        return "failed"

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
                          requested_at,responded_at,created_at,
                          invocation_id,attempt_ordinal,provenance_status
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
            if row[20] != "resolved" or row[18] is None or row[19] is None:
                raise SearchProvenanceError(
                    "existing search response is not relationally resolved"
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
            "invocation_id": row[18],
            "attempt_ordinal": row[19],
            "provenance_status": row[20],
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
            invocation_id=UUID(str(row[18])),
            attempt_ordinal=int(row[19]),
        )

    def _resolve_authority_context(
        self,
        run_id: UUID,
        context: AuthoritativeAcquisitionContext | None,
    ) -> AuthoritativeAcquisitionContext:
        if context is None:
            if self.authority_preflight is None or self.config is None:
                raise AcquisitionPreflightError(
                    "authoritative acquisition preflight is required before "
                    "provider execution"
                )
            context = self.authority_preflight(run_id=run_id, config=self.config)
        if context.run_id != run_id or context.lifecycle_revision is None:
            raise AcquisitionPreflightError(
                "authoritative acquisition requires a matching run-bound "
                "preflight context"
            )
        return context

    @staticmethod
    def _revalidate_authority(
        uow: Any,
        context: AuthoritativeAcquisitionContext,
        run_id: UUID,
    ) -> None:
        if context.run_id != run_id or context.lifecycle_revision is None:
            raise AcquisitionAuthorityChangedError(
                "authoritative search requires the matching run-bound preflight context"
            )
        with uow.connection.cursor() as cur:
            cur.execute(
                "SELECT state,lifecycle_revision FROM research_runs "
                "WHERE id=%s FOR UPDATE",
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
        """Reconcile successful response rows without materialized candidates."""
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
