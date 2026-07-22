"""Terminal-decision persistence service.

This module implements the ``TerminalDecisionService`` that persists
terminal decisions produced by the ``TerminalDecisionPolicy`` to the
``terminal_decisions`` table (migration 0015).

Key invariants:

* Decisions are append-only — no UPDATE/DELETE is permitted.
* Idempotency keys prevent duplicate decisions for the same run.
* The service is intentionally thin — it carries no evaluation logic.
  The ``TerminalDecisionPolicy`` owns evaluation; this service owns
  persistence.

Usage::

    service = TerminalDecisionService(uow_factory)
    record = service.record(
        run_id=run_id,
        run_revision=run,
        coverage_revision=coverage_revision,
        decision=decision,
        idempotency_key=f"terminal:{run_id}",
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from research_domain.models import (
    TerminalDecision,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TerminalDecisionError(ValueError):
    """A terminal-decision operation violated a constraint."""


class DuplicateTerminalDecisionError(TerminalDecisionError):
    """An idempotency key already exists for this run."""


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminalDecisionRecord:
    """Persisted terminal-decision record returned from the service."""

    id: UUID
    run_id: UUID
    decision_id: UUID
    run_revision: int
    coverage_revision: int
    outcome: str
    no_progress_signals: tuple[str, ...]
    unresolved_gap: str
    policy_version: str
    idempotency_key: str
    created_at: datetime

    @classmethod
    def from_decision(
        cls,
        id: UUID,
        decision: TerminalDecision,
        idempotency_key: str,
        created_at: datetime | None = None,
    ) -> "TerminalDecisionRecord":
        """Build a record from an in-memory ``TerminalDecision``."""
        now = created_at or utcnow()
        return cls(
            id=id,
            run_id=decision.run_id,
            decision_id=decision.decision_id,
            run_revision=decision.run_revision,
            coverage_revision=decision.coverage_revision,
            outcome=decision.outcome.value,
            no_progress_signals=tuple(s.value for s in decision.no_progress_signals),
            unresolved_gap=decision.unresolved_gap,
            policy_version=decision.policy_version,
            idempotency_key=idempotency_key,
            created_at=now,
        )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TerminalDecisionService:
    """Persist terminal decisions to the ``terminal_decisions`` table.

    Args:
        uow_factory: Callable that returns a SQLAlchemy ``Session``-like
            object with ``execute()``, ``commit()``, and ``rollback()``.
    """

    def __init__(self, uow_factory: Callable[[], Any]) -> None:
        self.uow_factory = uow_factory

    def record(
        self,
        run_id: UUID,
        decision: TerminalDecision,
        idempotency_key: str,
    ) -> TerminalDecisionRecord:
        """Persist a terminal decision to the ``terminal_decisions`` table.

        Args:
            run_id: The research run UUID.
            decision: The ``TerminalDecision`` produced by the policy.
            idempotency_key: Deduplication key — must be unique per run.

        Returns:
            A ``TerminalDecisionRecord`` with the persisted ID and timestamp.

        Raises:
            DuplicateTerminalDecisionError: If the idempotency key already
                exists for this run.
        """
        uow = self.uow_factory()
        try:
            created_at = utcnow()

            uow.execute(
                """INSERT INTO terminal_decisions (
                    run_id, decision_id, run_revision, coverage_revision,
                    outcome, no_progress_signals, unresolved_gap,
                    policy_version, idempotency_key, created_at
                ) VALUES (
                    :run_id, :decision_id, :run_revision, :coverage_revision,
                    :outcome, :signals, :unresolved_gap,
                    :policy_version, :idempotency_key, :created_at
                ) RETURNING id, created_at""",
                {
                    "run_id": str(run_id),
                    "decision_id": str(decision.decision_id),
                    "run_revision": decision.run_revision,
                    "coverage_revision": decision.coverage_revision,
                    "outcome": decision.outcome.value,
                    "signals": tuple(s.value for s in decision.no_progress_signals),
                    "unresolved_gap": decision.unresolved_gap,
                    "policy_version": decision.policy_version,
                    "idempotency_key": idempotency_key,
                    "created_at": created_at,
                },
            )
            row = uow.fetchone()
            uow.commit()

            return TerminalDecisionRecord.from_decision(
                id=row[0],
                decision=decision,
                idempotency_key=idempotency_key,
                created_at=row[1],
            )
        except Exception as exc:
            uow.rollback()
            # Check for unique violation (duplicate idempotency key)
            detail = str(exc)
            if (
                "uk_terminal_decisions_idempotency" in detail
                or "duplicate" in detail.lower()
            ):
                raise DuplicateTerminalDecisionError(
                    f"terminal decision already recorded for run {run_id}"
                ) from exc
            raise
