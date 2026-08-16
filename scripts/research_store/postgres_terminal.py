"""Connection-bound PostgreSQL terminal-decision persistence for issue #257."""

from __future__ import annotations

from typing import Any


class PostgresTerminalDecisionRepository:
    """Terminal provenance persistence on the exact containing UoW connection.

    This repository deliberately does not commit, rollback, or independently
    transition the run.  ``ResearchRunService.commit_terminal_decision`` binds
    this insert to the matching run transition inside one UoW transaction.
    """

    def __init__(self, connection: Any) -> None:
        self.__connection = connection

    def record_terminal_decision(
        self,
        run_id,
        decision_id,
        run_revision,
        coverage_revision,
        outcome,
        no_progress_signals,
        unresolved_gap,
        policy_version,
        idempotency_key,
        created_at,
    ) -> dict[str, Any]:
        no_progress_signals_list = (
            list(no_progress_signals) if no_progress_signals else []
        )
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, decision_id, run_revision, coverage_revision,
                          outcome, no_progress_signals, unresolved_gap,
                          policy_version, idempotency_key, created_at
                   FROM terminal_decisions
                   WHERE run_id = %s AND idempotency_key = %s""",
                (run_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                return {
                    "decision_id": existing[1],
                    "id": existing[0],
                    "created_at": existing[9],
                    "reused": True,
                }
            cur.execute(
                """INSERT INTO terminal_decisions (
                    run_id, decision_id, run_revision, coverage_revision,
                    outcome, no_progress_signals, unresolved_gap,
                    policy_version, idempotency_key, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, created_at""",
                (
                    run_id,
                    decision_id,
                    run_revision,
                    coverage_revision,
                    outcome,
                    no_progress_signals_list,
                    unresolved_gap,
                    policy_version,
                    idempotency_key,
                    created_at,
                ),
            )
            row = cur.fetchone()
            return {
                "decision_id": decision_id,
                "id": row[0],
                "created_at": row[1],
                "reused": False,
            }
