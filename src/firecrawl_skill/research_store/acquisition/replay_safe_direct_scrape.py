"""Replay-safe hard-budget admission for authoritative direct scrape."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from .direct_scrape_application import (
    DirectScrapeExtractionBudgetError,
    DirectScrapeService,
)
from .models import DirectScrapeBatchResult, DirectScrapeRequest


class ReplaySafeDirectScrapeService(DirectScrapeService):
    """Close the replay-at-budget race without changing scrape persistence.

    The base service checks terminal replay before entering the run-scoped hard
    budget lock. A concurrent identical caller can observe no terminal row,
    wait behind the first caller, then acquire the lock after the first caller
    has completed. Rechecking terminal identity *inside* that lock is required
    before projecting fresh work; otherwise a zero-cost replay can be rejected
    at the hard cap.
    """

    _active_batch_key: ContextVar[str | None] = ContextVar(
        "direct_scrape_active_batch_key", default=None
    )

    def logical_idempotency_key(
        self, run_id: UUID | str, requests: Sequence[DirectScrapeRequest]
    ) -> str:
        """Return the base service's canonical normalized logical request key."""
        run_uuid = UUID(str(run_id))
        normalized = tuple(self._normalize_request(item) for item in requests)
        return self._default_idempotency_key(run_uuid, normalized)

    def latest_terminal_logical_invocation(
        self,
        run_id: UUID | str,
        requests: Sequence[DirectScrapeRequest],
        *,
        exclude_idempotency_key: str | None = None,
    ) -> UUID | None:
        """Return the newest terminal invocation with the same logical input.

        Fresh work deliberately uses a unique idempotency key, so lineage
        cannot be recovered from the key.  The normalized immutable invocation
        input is the authoritative equivalence relation across ordinary, fresh,
        and prior retry executions.
        """
        run_uuid = UUID(str(run_id))
        normalized = tuple(self._normalize_request(item) for item in requests)
        expected_input = self._invocation_input(normalized)
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT id FROM research_invocations
                     WHERE run_id=%s AND operation='direct_scrape'
                       AND input=%s::jsonb
                       AND status IN ('complete','partial','failed')
                       AND (%s IS NULL OR idempotency_key<>%s)
                     ORDER BY created_at DESC,id DESC LIMIT 1""",
                (
                    run_uuid,
                    json.dumps(expected_input, sort_keys=True),
                    exclude_idempotency_key,
                    exclude_idempotency_key,
                ),
            )
            row = cursor.fetchone()
        return None if row is None else UUID(str(row[0]))

    def invocation_parent(
        self, run_id: UUID | str, invocation_id: UUID | str
    ) -> UUID | None:
        """Return the immutable persisted parent for one direct-scrape invocation."""
        run_uuid = UUID(str(run_id))
        invocation_uuid = UUID(str(invocation_id))
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT parent_invocation_id FROM research_invocations
                     WHERE id=%s AND run_id=%s AND operation='direct_scrape'""",
                (invocation_uuid, run_uuid),
            )
            row = cursor.fetchone()
        if row is None:
            raise RuntimeError(
                f"direct scrape invocation disappeared before lineage readback: {invocation_uuid}"
            )
        return None if row[0] is None else UUID(str(row[0]))

    def record_fresh_invocation_lineage(
        self,
        run_id: UUID | str,
        invocation_id: UUID | str,
        *,
        logical_idempotency_key: str,
        parent_invocation_id: UUID | str | None,
    ) -> None:
        """Append immutable operator provenance for an effective fresh run."""
        run_uuid = UUID(str(run_id))
        invocation_uuid = UUID(str(invocation_id))
        parent_uuid = (
            UUID(str(parent_invocation_id))
            if parent_invocation_id is not None
            else None
        )
        with self.uow_factory() as uow:
            uow.runs.append_event(
                run_uuid,
                "direct_scrape_fresh_executed",
                "operator",
                f"fresh-lineage:{invocation_uuid}",
                invocation_id=invocation_uuid,
                payload={
                    "work_mode": "fresh",
                    "logical_idempotency_key": logical_idempotency_key,
                    "parent_invocation_id": (
                        str(parent_uuid) if parent_uuid is not None else None
                    ),
                },
            )

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
        run_uuid = UUID(str(run_id))
        normalized = tuple(self._normalize_request(item) for item in requests)
        batch_key = idempotency_key or self._default_idempotency_key(
            run_uuid, normalized
        )
        token = self._active_batch_key.set(batch_key)
        try:
            return super().execute(
                run_uuid,
                requests,
                idempotency_key=batch_key,
                external_invocation_id=external_invocation_id,
                parent_invocation_id=parent_invocation_id,
                retry_parent_attempt_ids=retry_parent_attempt_ids,
            )
        finally:
            self._active_batch_key.reset(token)

    @contextmanager
    def _budget_guard(self, run_id: UUID, new_attempts: int) -> Iterator[None]:
        limit = self.budget.max_exploratory_extraction_attempts
        batch_key = self._active_batch_key.get()
        with self.uow_factory() as uow:
            lock_key = self._budget_lock_key(run_id)
            with uow.connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
            uow.connection.commit()
            try:
                terminal_replay = False
                current_count = 0
                projected_attempts = new_attempts
                with uow.connection.cursor() as cursor:
                    if batch_key is not None:
                        cursor.execute(
                            """SELECT status,output FROM research_invocations
                                 WHERE run_id=%s AND idempotency_key=%s
                                   AND operation='direct_scrape'""",
                            (run_id, batch_key),
                        )
                        invocation = cursor.fetchone()
                        if invocation is not None:
                            status, output = invocation
                            terminal_replay = status in {
                                "complete",
                                "partial",
                                "failed",
                                "cancelled",
                            }
                            if not terminal_replay:
                                saved = self._items_by_key(output)
                                projected_attempts = max(
                                    new_attempts - len(saved), 0
                                )
                    if not terminal_replay:
                        cursor.execute(
                            "SELECT count(*) FROM extraction_attempts WHERE run_id=%s",
                            (run_id,),
                        )
                        row = cursor.fetchone()
                        current_count = int(row[0]) if row is not None else 0
                uow.connection.commit()
                if terminal_replay:
                    yield
                    return
                projected_count = current_count + projected_attempts
                if projected_count > limit:
                    raise DirectScrapeExtractionBudgetError(
                        run_id=run_id,
                        current_count=current_count,
                        hard_limit=limit,
                        projected_count=projected_count,
                    )
                yield
            finally:
                with uow.connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
                uow.connection.commit()


__all__ = ["ReplaySafeDirectScrapeService"]
