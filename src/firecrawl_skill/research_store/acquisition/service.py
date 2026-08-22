"""PostgreSQL-authoritative search acquisition application service."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any
from uuid import UUID

from ..blob import ContentAddressedBlobStore
from ..recency import validate_recency_window
from .authority import (
    ACQUISITION_ENTRY_STATES,
    AcquisitionPreflightError,
    AuthoritativeAcquisitionContext,
)
from .models import AcquisitionResult, SearchAdapterResult
from .ports import SearchAdapter

POSTGRES_INTEGER_MAX = 2_147_483_647
DEFAULT_IDEMPOTENCY_LOCK_TIMEOUT_SECONDS = 5.0
DEFAULT_IDEMPOTENCY_LOCK_POLL_SECONDS = 0.05
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|authorization|password|secret|credential)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


def _redact_error_text(value: object, *, max_chars: int = 1000) -> str:
    text = str(value)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        text,
    )
    return text[:max_chars]


def _redact_diagnostic_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_error_text(value)
    if isinstance(value, Mapping):
        return {str(key): _redact_diagnostic_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_diagnostic_value(item) for item in value]
    return value


class AcquisitionAuthorityChangedError(RuntimeError):
    """The preflight authority snapshot became stale before commit."""


class AcquisitionIdempotencyConflictError(RuntimeError):
    """An idempotency key was reused for a different search request."""


class AcquisitionConcurrencyError(RuntimeError):
    """A bounded search-idempotency lock acquisition could not complete."""

    reason_code = "search_idempotency_lock_timeout"


class SearchProvenanceError(RuntimeError):
    """Search provenance could not be established without guessing."""


class AcquisitionService:
    """Execute provider searches with PostgreSQL-authoritative provenance.

    The application service owns persistence/idempotency/provenance policy and
    depends only on :class:`SearchAdapter`. Concrete provider selection belongs
    to composition roots.
    """

    def __init__(
        self,
        uow_factory: Callable,
        blob_store: Any | None = None,
        search_adapter: SearchAdapter | None = None,
        *,
        config: Any | None = None,
        authority_preflight: Callable[..., AuthoritativeAcquisitionContext]
        | None = None,
        idempotency_lock_timeout_seconds: float = (
            DEFAULT_IDEMPOTENCY_LOCK_TIMEOUT_SECONDS
        ),
        idempotency_lock_poll_seconds: float = DEFAULT_IDEMPOTENCY_LOCK_POLL_SECONDS,
    ):
        if idempotency_lock_timeout_seconds <= 0:
            raise ValueError("idempotency lock timeout must be positive")
        if idempotency_lock_poll_seconds <= 0:
            raise ValueError("idempotency lock poll interval must be positive")
        self.uow_factory = uow_factory
        self.blob_store = blob_store
        self.search_adapter = search_adapter
        self.config = config
        self.authority_preflight = authority_preflight
        self.idempotency_lock_timeout_seconds = float(idempotency_lock_timeout_seconds)
        self.idempotency_lock_poll_seconds = float(idempotency_lock_poll_seconds)

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
        validate_recency_window(tbs)

        inherited_parent = (metadata or {}).get("invocation_id")
        if parent_invocation_id is None and inherited_parent:
            parent_invocation_id = UUID(str(inherited_parent))
        elif parent_invocation_id is not None:
            parent_invocation_id = UUID(str(parent_invocation_id))

        authority_context = self._resolve_authority_context(run_id, authority_context)
        adapter = self.search_adapter
        if adapter is None:
            raise RuntimeError(
                "AcquisitionService requires an explicit SearchAdapter; "
                "select provider transport at a composition boundary"
            )

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
                adapter_result = adapter.search(
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
                    error=f"{type(exc).__name__}: {_redact_error_text(exc)}",
                )
                raise

            cancelled = bool(
                (adapter_result.transport_metadata or {}).get("cancelled", False)
            )
            try:
                attempt_ordinal = self._provider_attempt_ordinal(adapter_result)
            except BaseException as exc:
                self._terminalize_without_response(
                    run_id,
                    provider_invocation_id,
                    plan_id=plan_id,
                    plan_query_id=plan_query_id,
                    cancelled=cancelled
                    or isinstance(exc, (KeyboardInterrupt, SystemExit)),
                    error=f"{type(exc).__name__}: {_redact_error_text(exc)}",
                )
                raise

            safe_transport_error = (
                _redact_error_text(adapter_result.transport_error)
                if adapter_result.transport_error
                else None
            )
            safe_transport_metadata = _redact_diagnostic_value(
                adapter_result.transport_metadata or {}
            )
            postgres_committed = False
            event_id = None
            candidates: list[dict[str, Any]] = []
            resp_data: dict[str, Any] = {}
            try:
                with self.uow_factory() as uow:
                    self._revalidate_authority(uow, authority_context, run_id)
                    persisted_metadata = dict(metadata or {})
                    persisted_metadata.pop("invocation_id", None)
                    persisted_metadata["request_envelope"] = request_envelope
                    persisted_metadata["provider_invocation_id"] = str(
                        provider_invocation_id
                    )
                    persisted_metadata["attempt_ordinal"] = attempt_ordinal

                    resp_data = uow.search_responses.record_search_response(
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
                        error_message=safe_transport_error,
                        requested_at=adapter_result.requested_at,
                        responded_at=adapter_result.responded_at,
                        transport_metadata=safe_transport_metadata,
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
                               RETURNING invocation_id,attempt_ordinal,
                                     provenance_status""",
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
                                            str(plan_query_id)
                                            if plan_query_id
                                            else None
                                        ),
                                    }
                                ),
                                safe_transport_error,
                                provider_invocation_id,
                                run_id,
                            ),
                        )
                        if cur.fetchone() is None:
                            raise SearchProvenanceError(
                                "provider invocation was not running at response commit"
                            )

                    if resp_data["status"] in ("succeeded", "empty"):
                        candidates = uow.candidates.record_response_candidates(
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
            except BaseException as exc:
                try:
                    self._terminalize_without_response(
                        run_id,
                        provider_invocation_id,
                        plan_id=plan_id,
                        plan_query_id=plan_query_id,
                        cancelled=cancelled
                        or isinstance(exc, (KeyboardInterrupt, SystemExit)),
                        error=f"{type(exc).__name__}: {_redact_error_text(exc)}",
                    )
                except Exception as cleanup_exc:  # noqa: BLE001
                    raise SearchProvenanceError(
                        "search response persistence failed and provider-attempt "
                        "terminalization also failed: "
                        f"{type(cleanup_exc).__name__}: "
                        f"{_redact_error_text(cleanup_exc)}"
                    ) from exc
                raise

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
        safe_error = _redact_error_text(error)
        with self.uow_factory() as uow:
            with uow.connection.cursor() as cur:
                cur.execute(
                    """SELECT 1
                       FROM search_responses
                       WHERE run_id=%s AND invocation_id=%s
                         AND provenance_status='resolved'
                       LIMIT 1""",
                    (run_id, invocation_id),
                )
                if cur.fetchone() is not None:
                    return

                cur.execute(
                    "SELECT state FROM research_runs WHERE id=%s FOR UPDATE",
                    (run_id,),
                )
                run_row = cur.fetchone()
                run_cancelled = run_row is not None and str(run_row[0]) == "cancelled"
                terminal_state = "cancelled" if cancelled or run_cancelled else "failed"
                reason_code = (
                    "provider_attempt_cancelled"
                    if terminal_state == "cancelled"
                    else "provider_attempt_failed_without_response"
                )

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
                       SET status=%s,
                           output=%s::jsonb,
                           error=%s,
                           completed_at=now()
                       WHERE id=%s AND run_id=%s AND status='running'""",
                    (
                        terminal_state,
                        json.dumps(
                            {
                                "status": "no_response",
                                "reason_code": reason_code,
                                "plan_id": str(plan_id) if plan_id else None,
                                "plan_query_id": (
                                    str(plan_query_id) if plan_query_id else None
                                ),
                            }
                        ),
                        safe_error,
                        invocation_id,
                        run_id,
                    ),
                )
            uow.commit()

    @staticmethod
    def _provider_attempt_ordinal(adapter_result: SearchAdapterResult) -> int:
        metadata = adapter_result.transport_metadata or {}
        explicit_values: list[tuple[str, int]] = []
        for field_name in ("attempt", "attempts"):
            raw = metadata.get(field_name)
            if raw is None:
                continue
            if isinstance(raw, bool):
                raise SearchProvenanceError(
                    "provider attempt metadata must contain a positive 32-bit "
                    f"integer in {field_name}"
                )
            try:
                value = int(raw)
            except (TypeError, ValueError) as exc:
                raise SearchProvenanceError(
                    "provider attempt metadata must contain a positive 32-bit "
                    f"integer in {field_name}"
                ) from exc
            if not 0 < value <= POSTGRES_INTEGER_MAX:
                raise SearchProvenanceError(
                    "provider attempt metadata must contain a positive 32-bit "
                    f"integer in {field_name}"
                )
            explicit_values.append((field_name, value))
        if not explicit_values:
            return 1
        ordinals = {value for _field_name, value in explicit_values}
        if len(ordinals) != 1:
            raise SearchProvenanceError(
                "provider attempt metadata contains conflicting attempt ordinals"
            )
        return explicit_values[0][1]

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
        deadline = time.monotonic() + self.idempotency_lock_timeout_seconds
        acquired = False
        with self.uow_factory() as uow:
            while not acquired:
                with uow.connection.cursor() as cur:
                    cur.execute(
                        "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                        (lock_name,),
                    )
                    acquired = bool(cur.fetchone()[0])
                uow.connection.commit()
                if acquired:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AcquisitionConcurrencyError(
                        "authoritative search idempotency lock was not acquired "
                        f"within {self.idempotency_lock_timeout_seconds:.3f}s; "
                        f"reason_code={AcquisitionConcurrencyError.reason_code}"
                    )
                time.sleep(min(self.idempotency_lock_poll_seconds, remaining))
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
        authority_context: AuthoritativeAcquisitionContext,
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
            responses = uow.search_responses.list_search_responses(run_id)
            for resp in responses:
                if resp["status"] in ("succeeded", "empty"):
                    cands = uow.candidates.record_response_candidates(
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


__all__ = [
    "AcquisitionAuthorityChangedError",
    "AcquisitionConcurrencyError",
    "AcquisitionIdempotencyConflictError",
    "AcquisitionResult",
    "AcquisitionService",
    "SearchProvenanceError",
]
