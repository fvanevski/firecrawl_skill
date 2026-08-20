"""Connection-bound PostgreSQL terminal-decision persistence for issue #257."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


class PostgresTerminalDecisionRepository:
    """Terminal provenance persistence on the exact containing UoW connection.

    This repository deliberately does not commit, rollback, or independently
    transition the run. ``GuardedResearchRunService.commit_terminal_decision``
    binds this insert to the matching run transition inside one UoW transaction.
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
        *,
        reason_code: str | None = None,
        state_census: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_reason_code = reason_code or "policy_terminal_decision"
        if not resolved_reason_code.strip():
            raise ValueError("terminal decision reason_code is required")
        if resolved_reason_code == "legacy_unstructured":
            raise ValueError("new terminal decisions require structured provenance")
        census: Mapping[str, Any] = state_census or {
            "schema_version": "terminal-state-census-v1",
            "available": False,
            "reason": "not_supplied_by_caller",
        }
        if not isinstance(census, Mapping):
            raise TypeError("terminal decision state_census must be an object")
        census_dict = dict(census)
        no_progress_signals_list = (
            list(no_progress_signals) if no_progress_signals else []
        )
        expected = (
            str(decision_id),
            int(run_revision),
            int(coverage_revision),
            str(outcome),
            no_progress_signals_list,
            unresolved_gap,
            policy_version,
            resolved_reason_code,
            census_dict,
        )
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO terminal_decisions(
                       run_id,decision_id,run_revision,coverage_revision,
                       outcome,no_progress_signals,unresolved_gap,
                       policy_version,idempotency_key,created_at,
                       reason_code,state_census)
                     VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                     ON CONFLICT(run_id,idempotency_key) DO NOTHING
                     RETURNING id,created_at""",
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
                    resolved_reason_code,
                    json.dumps(census_dict, sort_keys=True),
                ),
            )
            inserted = cur.fetchone()
            if inserted is not None:
                return {
                    "decision_id": decision_id,
                    "id": inserted[0],
                    "created_at": inserted[1],
                    "reused": False,
                }

            cur.execute(
                """SELECT id,decision_id,run_revision,coverage_revision,
                          outcome,no_progress_signals,unresolved_gap,
                          policy_version,reason_code,state_census,created_at
                     FROM terminal_decisions
                    WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is None:
                raise RuntimeError(
                    "terminal decision idempotency conflict was not readable"
                )
            normalized_existing = (
                str(existing[1]),
                int(existing[2]),
                int(existing[3]),
                str(existing[4]),
                list(existing[5] or ()),
                existing[6],
                existing[7],
                existing[8],
                existing[9],
            )
            if normalized_existing != expected:
                raise ValueError(
                    "idempotency key was used for another terminal decision"
                )
            return {
                "decision_id": existing[1],
                "id": existing[0],
                "created_at": existing[10],
                "reused": True,
            }
