"""Exact-recency wrapper for search acquisition.

The provider receives only its documented coarse qdr filter. PostgreSQL and
local ranking retain the exact operator window, and candidate temporal
normalization happens transactionally in the canonical candidate repository.
When a caller omits ``tbs`` for a query that belongs to a persisted bounded
SearchPlan, the persisted plan supplies the exact recency requirement rather
than allowing the provider call to become silently unbounded.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from uuid import UUID

from ..plan_recency import plan_query_recency_tbs
from ..recency import RecencyWindow, normalize_recency_window
from ..temporal_candidate import ranking_safe_raw_item
from .models import AcquisitionResult
from .service import AcquisitionIdempotencyConflictError


class TemporalAcquisitionService:
    """Keep exact recency semantics authoritative around AcquisitionService."""

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.uow_factory = delegate.uow_factory

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    @property
    def idempotency_lock_timeout_seconds(self) -> float:
        """Preserve the delegate's public contention-control surface."""
        return float(self.delegate.idempotency_lock_timeout_seconds)

    @idempotency_lock_timeout_seconds.setter
    def idempotency_lock_timeout_seconds(self, value: float) -> None:
        self.delegate.idempotency_lock_timeout_seconds = value

    @property
    def idempotency_lock_poll_seconds(self) -> float:
        """Preserve the delegate's public contention-control surface."""
        return float(self.delegate.idempotency_lock_poll_seconds)

    @idempotency_lock_poll_seconds.setter
    def idempotency_lock_poll_seconds(self, value: float) -> None:
        self.delegate.idempotency_lock_poll_seconds = value

    @staticmethod
    def _default_key(
        run_id: UUID,
        query_text: str,
        *,
        plan_query_id: UUID | None,
        limit: int,
        sources: str,
        tbs: str | None,
    ) -> str:
        payload = {
            "query_text": query_text,
            "plan_query_id": str(plan_query_id) if plan_query_id else None,
            "limit": int(limit),
            "sources": sources,
            "tbs": tbs,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return f"search:{run_id}:{digest}"

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
        authority_context: Any | None = None,
        replay_existing: bool = True,
    ) -> AcquisitionResult:
        run_uuid = UUID(str(run_id))
        if tbs is None:
            tbs = self._persisted_plan_recency(
                run_uuid,
                query_text,
                plan_query_id=plan_query_id,
            )
        window = normalize_recency_window(tbs)
        provider_tbs = window.provider_tbs if window is not None else None
        persisted_metadata = dict(metadata or {})
        if window is not None:
            persisted_metadata["recency"] = window.to_dict()
        key = idempotency_key or self._default_key(
            run_uuid,
            query_text,
            plan_query_id=(
                UUID(str(plan_query_id)) if plan_query_id is not None else None
            ),
            limit=limit,
            sources=sources,
            tbs=tbs,
        )
        self._assert_exact_replay_semantics(run_uuid, key, window)
        result = self.delegate.execute_search(
            run_uuid,
            query_text,
            backend=backend,
            plan_id=plan_id,
            plan_query_id=plan_query_id,
            parent_invocation_id=parent_invocation_id,
            idempotency_key=key,
            limit=limit,
            sources=sources,
            tbs=provider_tbs,
            metadata=persisted_metadata,
            authority_context=authority_context,
            replay_existing=replay_existing,
        )
        candidates = self._ranking_safe_occurrences(result.candidates)
        response = dict(result.search_response)
        if window is not None:
            response["recency"] = window.to_dict()
        return replace(
            result,
            candidates=candidates,
            candidate_count=len(candidates),
            search_response=response,
        )

    def _persisted_plan_recency(
        self,
        run_id: UUID,
        query_text: str,
        *,
        plan_query_id: UUID | None,
    ) -> str | None:
        """Resolve one query's canonical persisted TimeWindow, if any."""
        with self.uow_factory() as uow:
            if plan_query_id is not None:
                row = uow.search_responses.get_plan_query(
                    UUID(str(plan_query_id)),
                    run_id=run_id,
                )
            else:
                try:
                    plan = uow.search_responses.get_search_plan(run_id)
                except (KeyError, ValueError):
                    return None
                matches = [
                    item
                    for item in plan.get("queries", ())
                    if item.get("query_text") == query_text
                ]
                if not matches:
                    return None
                if len(matches) != 1:
                    raise ValueError(
                        "persisted search plan contains ambiguous query text"
                    )
                row = matches[0]
        return plan_query_recency_tbs(
            {"freshness_requirement": row.get("freshness_requirement")}
        )

    def _assert_exact_replay_semantics(
        self,
        run_id: UUID,
        idempotency_key: str,
        window: RecencyWindow | None,
    ) -> None:
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT transport_metadata FROM search_responses
                     WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            row = cursor.fetchone()
        if row is None:
            return
        stored = row[0] or {}
        stored_recency = stored.get("recency") if isinstance(stored, dict) else None
        requested = window.requested_tbs if window is not None else None
        stored_requested = (
            stored_recency.get("requested_tbs")
            if isinstance(stored_recency, dict)
            else None
        )
        if stored_requested != requested:
            raise AcquisitionIdempotencyConflictError(
                "search idempotency key was used with different exact recency semantics"
            )

    @staticmethod
    def _ranking_safe_occurrences(
        occurrences: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for occurrence in occurrences:
            raw = occurrence.get("raw_item") or {}
            safe = ranking_safe_raw_item(raw) if isinstance(raw, Mapping) else {}
            result.append({**occurrence, "raw_item": safe})
        return result

    def reconcile_pending_searches(self, run_id: UUID) -> list[dict[str, Any]]:
        # Candidate canonicalization is performed by the candidate repository in
        # the same UoW that materializes reconciled occurrences.
        return self.delegate.reconcile_pending_searches(run_id)


__all__ = ["TemporalAcquisitionService"]
