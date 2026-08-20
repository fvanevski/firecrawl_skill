"""Connection-bound PostgreSQL strategy-revision persistence for issue #257."""

from __future__ import annotations

import json
from typing import Any

from .postgres_research import _lock_workflow_run


class PostgresStrategyRevisionRepository:
    """Strategy proposal/decision persistence on the containing UoW connection."""

    def __init__(self, connection: Any) -> None:
        self.__connection = connection

    @staticmethod
    def _row_to_proposal_mapping(row):
        return {
            "id": str(row[0]),
            "run_id": str(row[1]),
            "run_revision": row[2],
            "coverage_revision": row[3],
            "revision_order": row[4],
            "row_type": row[5],
            "proposal_id": str(row[6]),
            "decision_type": row[7],
            "target_coverage_item_ids": row[8] or [],
            "proposed_queries": row[9] or [],
            "proposed_candidate_ids": row[10] or [],
            "proposed_retrieval_queries": row[11] or [],
            "expected_contribution": row[12] or "",
            "estimated_cost": row[13] or {},
            "rationale": row[14] or "",
            "confidence": row[15] or 0.0,
            "idempotency_key": row[16],
            "actor_type": row[17] or "system",
            "actor_identifier": row[18],
            "created_at": row[19],
        }

    @staticmethod
    def _row_to_decision_mapping(row):
        return {
            "id": str(row[0]),
            "run_id": str(row[1]),
            "run_revision": row[2],
            "coverage_revision": row[3],
            "revision_order": row[4],
            "row_type": row[5],
            "proposal_id": str(row[6]),
            "decision_id": str(row[7]),
            "outcome": row[8],
            "rejection_reasons": row[9] or [],
            "policy_version": row[10] or "",
            "scope_expansion_type": row[11],
            "scope_expansion_rationale": row[12],
            "scope_expansion_approved": row[13],
            "authorized_by": row[14] or "",
            "idempotency_key": row[15],
            "actor_type": row[16] or "system",
            "actor_identifier": row[17],
            "created_at": row[18],
        }

    def _get_proposal_row(self, cur, run_id, proposal_id):
        cur.execute(
            """SELECT id, run_id, run_revision, coverage_revision,
                revision_order, row_type, proposal_id, decision_type,
                target_coverage_item_ids, proposed_queries,
                proposed_candidate_ids, proposed_retrieval_queries,
                expected_contribution, estimated_cost, rationale, confidence,
                idempotency_key, actor_type, actor_identifier, created_at
            FROM strategy_revisions
            WHERE run_id=%s AND proposal_id=%s AND row_type='proposal'
            ORDER BY created_at DESC LIMIT 1""",
            (run_id, proposal_id),
        )
        row = cur.fetchone()
        return None if row is None else self._row_to_proposal_mapping(row)

    def _get_decision_row(self, cur, run_id, decision_id):
        cur.execute(
            """SELECT id, run_id, run_revision, coverage_revision,
                revision_order, row_type, proposal_id, decision_id,
                outcome, rejection_reasons, policy_version,
                scope_expansion_type, scope_expansion_rationale,
                scope_expansion_approved, authorized_by,
                idempotency_key, actor_type, actor_identifier, created_at
            FROM strategy_revisions
            WHERE run_id=%s AND decision_id=%s AND row_type='decision'
            ORDER BY created_at DESC LIMIT 1""",
            (run_id, decision_id),
        )
        row = cur.fetchone()
        return None if row is None else self._row_to_decision_mapping(row)

    def record_proposal(
        self,
        run_id,
        proposal_id,
        run_revision,
        coverage_revision,
        decision_type,
        target_coverage_item_ids,
        proposed_queries,
        proposed_candidate_ids,
        proposed_retrieval_queries,
        expected_contribution,
        estimated_cost,
        rationale,
        confidence,
        idempotency_key,
        **metadata,
    ):
        with self.__connection.cursor() as cur:
            _lock_workflow_run(cur, run_id)
            cur.execute(
                """INSERT INTO strategy_revisions(
                    run_id, run_revision, coverage_revision,
                    revision_order, row_type, proposal_id, decision_type,
                    target_coverage_item_ids, proposed_queries,
                    proposed_candidate_ids, proposed_retrieval_queries,
                    expected_contribution, estimated_cost, rationale, confidence,
                    idempotency_key, actor_type, actor_identifier
                ) VALUES(%s, %s, %s,
                    (SELECT COALESCE(MAX(revision_order), 0) + 1
                     FROM strategy_revisions WHERE run_id=%s),
                    'proposal', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s)
                ON CONFLICT(run_id, idempotency_key) DO NOTHING
                RETURNING id, run_id, run_revision, coverage_revision,
                    revision_order, row_type, proposal_id, decision_type,
                    target_coverage_item_ids, proposed_queries,
                    proposed_candidate_ids, proposed_retrieval_queries,
                    expected_contribution, estimated_cost, rationale, confidence,
                    idempotency_key, actor_type, actor_identifier, created_at""",
                (
                    run_id,
                    run_revision,
                    coverage_revision,
                    run_id,
                    proposal_id,
                    decision_type,
                    json.dumps(target_coverage_item_ids),
                    json.dumps(proposed_queries),
                    json.dumps(proposed_candidate_ids),
                    json.dumps(proposed_retrieval_queries),
                    expected_contribution,
                    json.dumps(estimated_cost),
                    rationale,
                    confidence,
                    idempotency_key,
                    metadata.get("actor_type", "system"),
                    metadata.get("actor_identifier"),
                ),
            )
            row = cur.fetchone()
            if row is None:
                return self._get_proposal_row(cur, run_id, proposal_id)
            return self._row_to_proposal_mapping(row)

    def get_proposal(self, run_id, proposal_id):
        with self.__connection.cursor() as cur:
            return self._get_proposal_row(cur, run_id, proposal_id)

    def list_proposals(
        self,
        run_id,
        *,
        run_revision=None,
        coverage_revision=None,
        limit=100,
        offset=0,
    ):
        with self.__connection.cursor() as cur:
            where = "WHERE run_id=%s AND row_type='proposal'"
            params: list[Any] = [run_id]
            if run_revision is not None:
                where += " AND run_revision=%s"
                params.append(run_revision)
            if coverage_revision is not None:
                where += " AND coverage_revision=%s"
                params.append(coverage_revision)
            where += " ORDER BY revision_order DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])
            cur.execute(
                f"""SELECT id, run_id, run_revision, coverage_revision,
                    revision_order, row_type, proposal_id, decision_type,
                    target_coverage_item_ids, proposed_queries,
                    proposed_candidate_ids, proposed_retrieval_queries,
                    expected_contribution, estimated_cost, rationale, confidence,
                    idempotency_key, actor_type, actor_identifier, created_at
                FROM strategy_revisions {where}""",
                params,
            )
            return [self._row_to_proposal_mapping(row) for row in cur.fetchall()]

    def list_accepted_search_proposals(self, run_id) -> list[dict[str, Any]]:
        """Project accepted search proposals in persisted ascending revision order.

        Resume historically reconstructed this state with one SQL statement using
        ascending ``revision_order``. Keeping the projection in the canonical
        strategy repository preserves both the ordering and single-snapshot read
        semantics while preventing orchestration adapters from owning SQL.
        """
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT p.proposal_id, p.proposed_queries
                   FROM strategy_revisions p
                   WHERE p.run_id=%s
                     AND p.row_type='proposal'
                     AND p.decision_type='search'
                     AND EXISTS (
                       SELECT 1
                       FROM strategy_revisions d
                       WHERE d.run_id=p.run_id
                         AND d.row_type='decision'
                         AND d.proposal_id=p.proposal_id
                         AND d.outcome='accepted'
                     )
                   ORDER BY p.revision_order""",
                (run_id,),
            )
            rows = cur.fetchall()
        return [
            {
                "proposal_id": str(proposal_id),
                "decision_type": "search",
                "proposed_queries": list(queries or []),
            }
            for proposal_id, queries in rows
            if queries
        ]

    def record_decision(
        self,
        run_id,
        decision_id,
        proposal_id,
        run_revision,
        coverage_revision,
        outcome,
        rejection_reasons,
        policy_version,
        scope_expansion_type,
        scope_expansion_rationale,
        scope_expansion_approved,
        authorized_by,
        idempotency_key,
        **metadata,
    ):
        with self.__connection.cursor() as cur:
            _lock_workflow_run(cur, run_id)
            cur.execute(
                """INSERT INTO strategy_revisions(
                    run_id, run_revision, coverage_revision,
                    revision_order, row_type, proposal_id, decision_id,
                    outcome, rejection_reasons, policy_version,
                    scope_expansion_type, scope_expansion_rationale,
                    scope_expansion_approved, authorized_by,
                    idempotency_key, actor_type, actor_identifier
                ) VALUES(%s, %s, %s,
                    (SELECT COALESCE(MAX(revision_order), 0) + 1
                     FROM strategy_revisions WHERE run_id=%s),
                    'decision', %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s)
                ON CONFLICT(run_id, idempotency_key) DO NOTHING
                RETURNING id, run_id, run_revision, coverage_revision,
                    revision_order, row_type, proposal_id, decision_id,
                    outcome, rejection_reasons, policy_version,
                    scope_expansion_type, scope_expansion_rationale,
                    scope_expansion_approved, authorized_by,
                    idempotency_key, actor_type, actor_identifier, created_at""",
                (
                    run_id,
                    run_revision,
                    coverage_revision,
                    run_id,
                    proposal_id,
                    decision_id,
                    outcome,
                    rejection_reasons or [],
                    policy_version,
                    scope_expansion_type,
                    scope_expansion_rationale,
                    scope_expansion_approved,
                    authorized_by,
                    idempotency_key,
                    metadata.get("actor_type", "system"),
                    metadata.get("actor_identifier"),
                ),
            )
            row = cur.fetchone()
            if row is None:
                return self._get_decision_row(cur, run_id, decision_id)
            return self._row_to_decision_mapping(row)

    def get_decision(self, run_id, decision_id):
        with self.__connection.cursor() as cur:
            return self._get_decision_row(cur, run_id, decision_id)

    def list_decisions(
        self,
        run_id,
        *,
        proposal_id=None,
        outcome=None,
        limit=100,
        offset=0,
    ):
        with self.__connection.cursor() as cur:
            where = "WHERE run_id=%s AND row_type='decision'"
            params: list[Any] = [run_id]
            if proposal_id is not None:
                where += " AND proposal_id=%s"
                params.append(proposal_id)
            if outcome is not None:
                where += " AND outcome=%s"
                params.append(outcome)
            where += " ORDER BY revision_order DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])
            cur.execute(
                f"""SELECT id, run_id, run_revision, coverage_revision,
                    revision_order, row_type, proposal_id, decision_id,
                    outcome, rejection_reasons, policy_version,
                    scope_expansion_type, scope_expansion_rationale,
                    scope_expansion_approved, authorized_by,
                    idempotency_key, actor_type, actor_identifier, created_at
                FROM strategy_revisions {where}""",
                params,
            )
            return [self._row_to_decision_mapping(row) for row in cur.fetchall()]

    def proposal_exists(self, run_id, proposal_id):
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM strategy_revisions
                WHERE run_id=%s AND proposal_id=%s AND row_type='proposal'""",
                (run_id, proposal_id),
            )
            return cur.fetchone()[0] > 0

    def get_proposal_by_idempotency(self, run_id, idempotency_key):
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, run_revision, coverage_revision,
                    revision_order, row_type, proposal_id, decision_type,
                    target_coverage_item_ids, proposed_queries,
                    proposed_candidate_ids, proposed_retrieval_queries,
                    expected_contribution, estimated_cost, rationale, confidence,
                    idempotency_key, actor_type, actor_identifier, created_at
                FROM strategy_revisions
                WHERE run_id=%s AND idempotency_key=%s AND row_type='proposal'
                ORDER BY created_at DESC LIMIT 1""",
                (run_id, idempotency_key),
            )
            row = cur.fetchone()
            return None if row is None else self._row_to_proposal_mapping(row)

    def decision_exists(self, run_id, decision_id):
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM strategy_revisions
                WHERE run_id=%s AND decision_id=%s AND row_type='decision'""",
                (run_id, decision_id),
            )
            return cur.fetchone()[0] > 0

    def list_proposal_ids_for_run(self, run_id):
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT proposal_id FROM strategy_revisions
                WHERE run_id=%s AND row_type='proposal' ORDER BY proposal_id""",
                (run_id,),
            )
            return [str(row[0]) for row in cur.fetchall()]

    def list_decision_ids_for_proposal(self, run_id, proposal_id):
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT decision_id FROM strategy_revisions
                WHERE run_id=%s AND proposal_id=%s AND row_type='decision'
                ORDER BY revision_order""",
                (run_id, proposal_id),
            )
            return [str(row[0]) for row in cur.fetchall()]
