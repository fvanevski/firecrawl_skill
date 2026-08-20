"""Compatibility surface for terminal-decision policy records.

Authoritative terminal decisions are not standalone ledger writes. Migration
0039 binds every new decision to one semantically matching terminal lifecycle
transition in the same PostgreSQL transaction. Callers must therefore use
``ResearchRunService.commit_terminal_decision`` or a guarded terminal lifecycle
helper. This module retains the historical record type and a fail-closed
``record`` method so older integrations receive a precise migration error rather
than silently writing false provenance.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from firecrawl_skill.research_domain.models import TerminalDecision

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TerminalDecisionError(ValueError):
    """A terminal-decision operation violated an authoritative constraint."""


class DuplicateTerminalDecisionError(TerminalDecisionError):
    """An idempotency key already exists for another terminal command."""


@dataclass(frozen=True)
class TerminalDecisionRecord:
    """Persisted terminal-decision record returned by legacy readers."""

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
    ) -> TerminalDecisionRecord:
        """Build a compatibility record from an in-memory decision."""
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


class TerminalDecisionService:
    """Fail closed for the retired standalone decision-write API.

    Constructing the configured unit of work is intentional: connection and
    schema failures retain their original diagnostic context. Once a usable
    authoritative store is available, the method rejects before issuing SQL and
    directs the caller to the atomic lifecycle API.
    """

    def __init__(self, uow_factory: Callable[[], Any]) -> None:
        self.uow_factory = uow_factory

    def record(
        self,
        run_id: UUID,
        decision: TerminalDecision,
        idempotency_key: str,
    ) -> TerminalDecisionRecord:
        """Reject standalone persistence of a new terminal decision.

        Use ``ResearchRunService.commit_terminal_decision`` with the decision,
        target terminal state, reason code, and state census. The replacement
        API inserts the decision and transition under one run-scoped
        idempotency key and one PostgreSQL transaction.
        """
        del decision, idempotency_key
        try:
            with self.uow_factory():
                raise TerminalDecisionError(
                    "standalone terminal decision persistence is prohibited; "
                    "use ResearchRunService.commit_terminal_decision so the "
                    "decision and lifecycle transition commit atomically"
                )
        except DuplicateTerminalDecisionError:
            raise
        except TerminalDecisionError:
            raise
        except Exception as exc:
            logger.error(
                "terminal decision persistence preflight FAILED — aborting: %s",
                exc,
            )
            raise TerminalDecisionError(
                f"Failed to persist terminal decision for run {run_id}: {exc}"
            ) from exc
