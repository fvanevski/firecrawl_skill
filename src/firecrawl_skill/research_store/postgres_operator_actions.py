"""PostgreSQL persistence for durable operator actions and run lineage."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    return {}


class PostgresOperatorActionRepository:
    """Connection-bound repository for operator-action control-plane authority."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        keys = (
            "id",
            "external_action_id",
            "run_id",
            "run_external_id",
            "lifecycle_revision",
            "action_kind",
            "status",
            "policy_version",
            "authority_fingerprint",
            "creation_payload",
            "creation_sha256",
            "created_at",
            "resolution_id",
            "resolution_actor",
            "resolution_reason",
            "resolution_payload",
            "resolution_sha256",
            "resolved_at",
        )
        value = dict(zip(keys, row, strict=True))
        value["id"] = UUID(str(value["id"]))
        value["run_id"] = UUID(str(value["run_id"]))
        value["lifecycle_revision"] = int(value["lifecycle_revision"])
        value["creation_payload"] = _mapping(value["creation_payload"])
        value["resolution_payload"] = _mapping(value["resolution_payload"])
        if value["resolution_id"] is not None:
            value["resolution_id"] = UUID(str(value["resolution_id"]))
        return value

    @staticmethod
    def _columns() -> str:
        return """action.id,action.external_action_id,action.run_id,
                  run.external_run_id,action.lifecycle_revision,action.action_kind,
                  action.status,action.policy_version,action.authority_fingerprint,
                  action.creation_payload,action.creation_sha256,action.created_at,
                  action.resolution_id,action.resolution_actor,
                  action.resolution_reason,action.resolution_payload,
                  action.resolution_sha256,action.resolved_at"""

    def create_action(
        self,
        *,
        external_action_id: str,
        run_id: UUID,
        lifecycle_revision: int,
        action_kind: str,
        policy_version: str,
        authority_fingerprint: str,
        creation_payload: dict[str, Any],
        creation_sha256: str,
    ) -> dict[str, Any]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT lifecycle_revision FROM research_runs WHERE id=%s FOR UPDATE",
                (run_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(run_id)
            if int(row[0]) != lifecycle_revision:
                raise ValueError(
                    "operator action lifecycle revision is stale: "
                    f"expected {lifecycle_revision}, current {int(row[0])}"
                )
            cursor.execute(
                """INSERT INTO operator_actions(
                       external_action_id,run_id,lifecycle_revision,action_kind,status,
                       policy_version,authority_fingerprint,creation_payload,creation_sha256)
                     VALUES(%s,%s,%s,%s,'pending',%s,%s,%s::jsonb,%s)
                     ON CONFLICT(
                       run_id,lifecycle_revision,action_kind,authority_fingerprint
                     ) DO NOTHING
                     RETURNING id""",
                (
                    external_action_id,
                    run_id,
                    lifecycle_revision,
                    action_kind,
                    policy_version,
                    authority_fingerprint,
                    json.dumps(creation_payload, sort_keys=True),
                    creation_sha256,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                cursor.execute(
                    """SELECT creation_sha256 FROM operator_actions
                       WHERE run_id=%s AND lifecycle_revision=%s
                         AND action_kind=%s AND authority_fingerprint=%s""",
                    (
                        run_id,
                        lifecycle_revision,
                        action_kind,
                        authority_fingerprint,
                    ),
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise RuntimeError("operator action idempotency race")
                if str(existing[0]) != creation_sha256:
                    raise ValueError(
                        "operator action identity collides with different creation payload"
                    )
        if inserted is not None:
            return self.get_action(external_action_id=external_action_id)
        return self.get_matching_action(
            run_id,
            lifecycle_revision,
            action_kind,
            authority_fingerprint,
        )

    def get_matching_action(
        self,
        run_id: UUID,
        lifecycle_revision: int,
        action_kind: str,
        authority_fingerprint: str,
    ) -> dict[str, Any]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"""SELECT {self._columns()}
                    FROM operator_actions action
                    JOIN research_runs run ON run.id=action.run_id
                    WHERE action.run_id=%s AND action.lifecycle_revision=%s
                      AND action.action_kind=%s AND action.authority_fingerprint=%s""",
                (run_id, lifecycle_revision, action_kind, authority_fingerprint),
            )
            row = cursor.fetchone()
        if row is None:
            raise KeyError((run_id, action_kind, authority_fingerprint))
        return self._row(row)

    def get_action(
        self,
        *,
        external_action_id: str,
        for_update: bool = False,
    ) -> dict[str, Any]:
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"""SELECT {self._columns()}
                    FROM operator_actions action
                    JOIN research_runs run ON run.id=action.run_id
                    WHERE action.external_action_id=%s"""
                + (" FOR UPDATE OF action,run" if for_update else ""),
                (external_action_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise KeyError(external_action_id)
        return self._row(row)

    def pending_for_run(
        self,
        run_id: UUID,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                f"""SELECT {self._columns()}
                    FROM operator_actions action
                    JOIN research_runs run ON run.id=action.run_id
                    WHERE action.run_id=%s AND action.status='pending'"""
                + (" FOR UPDATE OF action,run" if for_update else ""),
                (run_id,),
            )
            rows = cursor.fetchall()
        if len(rows) > 1:
            raise RuntimeError("multiple pending operator actions exist for one run")
        return None if not rows else self._row(rows[0])

    def finish_action(
        self,
        action_id: UUID,
        *,
        status: str,
        resolution_id: UUID,
        resolution_actor: str,
        resolution_reason: str,
        resolution_payload: dict[str, Any],
        resolution_sha256: str,
    ) -> dict[str, Any]:
        if status not in {"resolved", "superseded"}:
            raise ValueError(f"unsupported operator action terminal status: {status}")
        with self._connection.cursor() as cursor:
            cursor.execute(
                """UPDATE operator_actions
                      SET status=%s,resolution_id=%s,resolution_actor=%s,
                          resolution_reason=%s,resolution_payload=%s::jsonb,
                          resolution_sha256=%s,resolved_at=now()
                    WHERE id=%s AND status='pending'""",
                (
                    status,
                    resolution_id,
                    resolution_actor,
                    resolution_reason,
                    json.dumps(resolution_payload, sort_keys=True),
                    resolution_sha256,
                    action_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("operator action is no longer pending")
            cursor.execute(
                "SELECT external_action_id FROM operator_actions WHERE id=%s",
                (action_id,),
            )
            external_action_id = str(cursor.fetchone()[0])
        return self.get_action(external_action_id=external_action_id)

    def record_lineage(
        self,
        *,
        child_run_id: UUID,
        parent_run_id: UUID,
        operator_action_id: UUID,
        parent_spec_id: UUID | None,
        parent_spec_revision: int | None,
        reason: str,
        child_objective: str,
    ) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO research_run_lineage(
                       child_run_id,parent_run_id,operator_action_id,parent_spec_id,
                       parent_spec_revision,reason,child_objective)
                     VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (
                    child_run_id,
                    parent_run_id,
                    operator_action_id,
                    parent_spec_id,
                    parent_spec_revision,
                    reason,
                    child_objective,
                ),
            )

    def lineage_for_child(self, child_run_id: UUID) -> dict[str, Any] | None:
        with self._connection.cursor() as cursor:
            cursor.execute(
                """SELECT child_run_id,parent_run_id,operator_action_id,
                          parent_spec_id,parent_spec_revision,reason,
                          child_objective,created_at
                     FROM research_run_lineage WHERE child_run_id=%s""",
                (child_run_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        keys = (
            "child_run_id",
            "parent_run_id",
            "operator_action_id",
            "parent_spec_id",
            "parent_spec_revision",
            "reason",
            "child_objective",
            "created_at",
        )
        return dict(zip(keys, row, strict=True))


__all__ = ["PostgresOperatorActionRepository"]
