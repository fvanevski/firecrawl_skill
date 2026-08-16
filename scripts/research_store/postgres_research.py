"""Connection-bound PostgreSQL research workflow persistence for issue #257.

This repository owns the durable run lifecycle, invocation/event journal, and
research-spec/budget persistence that define the core research-workflow state
boundary.  It receives the exact connection opened by ``PostgresUnitOfWork``;
it never owns connection lifecycle, commit, rollback, or savepoints.

Search-plan/acquisition persistence remains deliberately outside this module for
issue #258.  Evidence, audit, semantic-cache, and synthesis persistence remains
outside it for issue #259.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _lock_workflow_run(cur: Any, run_id: Any) -> tuple[Any, int]:
    """Lock one workflow run and return its state and lifecycle revision."""
    cur.execute(
        """SELECT state,lifecycle_revision FROM research_runs
        WHERE id=%s FOR UPDATE""",
        (run_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise KeyError(run_id)
    return row


class PostgresResearchRepository:
    """Canonical research-workflow persistence on one UoW-owned connection."""

    def __init__(self, connection: Any) -> None:
        self.__connection = connection

    def start_run(self, objective, metadata):
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs(objective,query_plan,skill_version,llm_model,
                retrieval_policy_version,external_run_id,state,
                execution_mode,budget_policy_version,metadata)
                VALUES(%s,%s,%s,%s,%s,%s,'created',%s,%s,%s)
                ON CONFLICT(external_run_id) DO NOTHING
                RETURNING id""",
                (
                    objective,
                    json.dumps(metadata.get("query_plan")),
                    metadata.get("skill_version"),
                    metadata.get("llm_model"),
                    metadata.get("policy_version"),
                    metadata.get("external_run_id"),
                    metadata.get("execution_mode", "agent_led"),
                    metadata.get("budget_policy_version"),
                    _canonical_json(metadata.get("metadata", {})),
                ),
            )
            inserted = cur.fetchone()
            if inserted is not None:
                return inserted[0]
            cur.execute(
                """SELECT id,objective,execution_mode FROM research_runs
                WHERE external_run_id=%s FOR UPDATE""",
                (metadata.get("external_run_id"),),
            )
            existing = cur.fetchone()
            if existing is None:
                raise RuntimeError("research run conflict could not be resolved")
            if existing[1:] != (
                objective,
                metadata.get("execution_mode", "agent_led"),
            ):
                raise ValueError("external run ID was used for another run")
            return existing[0]

    def get_run_status(self, *, run_id=None, external_id=None):
        if (run_id is None) == (external_id is None):
            raise ValueError("provide exactly one of run_id or external_id")
        with self.__connection.cursor() as cur:
            columns = """id,external_run_id,state,lifecycle_revision,
                reopened_from_revision,execution_mode,objective,declared_outcome,
                completed_at,error"""
            if run_id is not None:
                cur.execute(
                    f"SELECT {columns} FROM research_runs WHERE id=%s", (run_id,)
                )
            else:
                cur.execute(
                    f"SELECT {columns} FROM research_runs WHERE external_run_id=%s",
                    (external_id,),
                )
            row = cur.fetchone()
        if row is None:
            raise KeyError(run_id or external_id)
        keys = (
            "id",
            "external_id",
            "state",
            "lifecycle_revision",
            "reopened_from_revision",
            "execution_mode",
            "objective",
            "declared_outcome",
            "completed_at",
            "error",
        )
        return dict(zip(keys, row))

    def _lock_workflow_run(self, cur, run_id):
        return _lock_workflow_run(cur, run_id)

    def append_run_transition(
        self,
        run_id,
        lifecycle_revision,
        prior_state,
        next_state,
        idempotency_key,
        actor_type,
        policy_version,
        *,
        actor_identifier=None,
        triggering_event_id=None,
        semantic_proposal_id=None,
        validation_result=None,
        error=None,
    ):
        """Append one immutable transition row without state-machine policy."""
        with self.__connection.cursor() as cur:
            _lock_workflow_run(cur, run_id)
            validation_json = _canonical_json(validation_result or {})
            cur.execute(
                """SELECT id,lifecycle_revision,prior_state,next_state,
                triggering_event_id,actor_type,actor_identifier,policy_version,
                semantic_proposal_id,validation_result,error
                FROM research_run_transitions
                WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                expected = (
                    lifecycle_revision,
                    prior_state,
                    next_state,
                    triggering_event_id,
                    actor_type,
                    actor_identifier,
                    policy_version,
                    semantic_proposal_id,
                    json.loads(validation_json),
                    error,
                )
                if existing[1:] != expected:
                    raise ValueError("idempotency key was used for another transition")
                return {
                    "id": existing[0],
                    "lifecycle_revision": existing[1],
                    "prior_state": existing[2],
                    "next_state": existing[3],
                    "reused": True,
                }
            cur.execute(
                """INSERT INTO research_run_transitions(
                run_id,lifecycle_revision,prior_state,next_state,triggering_event_id,
                actor_type,actor_identifier,policy_version,semantic_proposal_id,
                validation_result,idempotency_key,error)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    run_id,
                    lifecycle_revision,
                    prior_state,
                    next_state,
                    triggering_event_id,
                    actor_type,
                    actor_identifier,
                    policy_version,
                    semantic_proposal_id,
                    validation_json,
                    idempotency_key,
                    error,
                ),
            )
            transition_id = cur.fetchone()[0]
            return {
                "id": transition_id,
                "lifecycle_revision": lifecycle_revision,
                "prior_state": prior_state,
                "next_state": next_state,
                "reused": False,
            }

    def apply_run_transition(
        self,
        run_id,
        next_state,
        expected_revision,
        idempotency_key,
        actor_type,
        policy_version,
        *,
        permitted_prior_states,
        actor_identifier=None,
        semantic_proposal_id=None,
        event_type,
        reason=None,
        outcome=None,
        error=None,
        completion=None,
        reopen=False,
    ):
        """Atomically lock, validate, record, and apply one lifecycle command."""
        completion = completion or {}
        command = {
            "expected_revision": expected_revision,
            "reason": reason,
            "outcome": outcome,
            "completion": completion,
            "reopen": reopen,
        }
        idempotent_command = {
            key: value for key, value in command.items() if key != "expected_revision"
        }
        event_payload = {
            "next_state": next_state,
            "expected_revision": expected_revision,
            "reason": reason,
            "outcome": outcome,
            "policy_version": policy_version,
        }
        with self.__connection.cursor() as cur:
            prior_state, current_revision = _lock_workflow_run(cur, run_id)
            cur.execute(
                """SELECT t.id,t.triggering_event_id,t.lifecycle_revision,
                t.prior_state,t.next_state,t.actor_type,t.actor_identifier,
                t.policy_version,t.semantic_proposal_id,t.validation_result,t.error,
                e.event_type,e.payload
                FROM research_run_transitions t
                JOIN research_events e ON e.id=t.triggering_event_id
                WHERE t.run_id=%s AND t.idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                expected = (
                    next_state,
                    actor_type,
                    actor_identifier,
                    policy_version,
                    semantic_proposal_id,
                    idempotent_command,
                    error,
                    event_type,
                )
                if existing[4:12] != expected:
                    raise ValueError("idempotency key was used for another run command")
                expected_payload = {
                    key: value
                    for key, value in (
                        event_payload | {"prior_state": existing[3]}
                    ).items()
                    if key != "expected_revision"
                }
                stored_payload = {
                    key: value
                    for key, value in existing[12].items()
                    if key != "expected_revision"
                }
                if stored_payload != expected_payload:
                    raise ValueError("idempotency key was used for another run command")
                return {
                    "transition_id": existing[0],
                    "event_id": existing[1],
                    "lifecycle_revision": existing[2],
                    "prior_state": existing[3],
                    "next_state": existing[4],
                    "reused": True,
                }
            if current_revision != expected_revision:
                raise ValueError(
                    "stale research run revision: "
                    f"expected {expected_revision}, current {current_revision}"
                )
            if prior_state not in permitted_prior_states:
                raise ValueError(
                    "research run transition rejected: "
                    f"{prior_state} -> {next_state} is not permitted"
                )
            if semantic_proposal_id is not None:
                cur.execute(
                    """SELECT validation_status,payload FROM semantic_artifacts
                    WHERE id=%s AND run_id=%s""",
                    (semantic_proposal_id, run_id),
                )
                proposal = cur.fetchone()
                if (
                    proposal is None
                    or proposal[0] != "valid"
                    or proposal[1].get("run_revision") != expected_revision
                ):
                    raise ValueError(
                        "semantic proposal is missing, cross-run, or stale"
                    )
            next_revision = current_revision + 1
            event_payload["prior_state"] = prior_state
            cur.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) FROM research_events WHERE run_id = %s",
                (run_id,),
            )
            next_seq = cur.fetchone()[0] + 1
            cur.execute(
                """INSERT INTO research_events(
                run_id,event_type,actor_type,actor_identifier,payload,
                run_revision,idempotency_key,sequence_number)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    run_id,
                    event_type,
                    actor_type,
                    actor_identifier,
                    _canonical_json(event_payload),
                    next_revision,
                    idempotency_key,
                    next_seq,
                ),
            )
            event_id = cur.fetchone()[0]
            cur.execute(
                """INSERT INTO research_run_transitions(
                run_id,lifecycle_revision,prior_state,next_state,triggering_event_id,
                actor_type,actor_identifier,policy_version,semantic_proposal_id,
                validation_result,idempotency_key,error)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id""",
                (
                    run_id,
                    next_revision,
                    prior_state,
                    next_state,
                    event_id,
                    actor_type,
                    actor_identifier,
                    policy_version,
                    semantic_proposal_id,
                    _canonical_json(idempotent_command),
                    idempotency_key,
                    error,
                ),
            )
            transition_id = cur.fetchone()[0]
            terminal = next_state in {"completed", "partial", "failed", "cancelled"}
            declared_outcome = outcome or {
                "completed": "satisfied",
                "partial": "partial",
                "failed": "failed",
                "cancelled": "cancelled",
            }.get(next_state)
            cur.execute(
                """UPDATE research_runs SET state=%s,lifecycle_revision=%s,
                reopened_from_revision=CASE WHEN %s THEN %s ELSE reopened_from_revision END,
                declared_outcome=%s,
                completed_at=CASE WHEN %s THEN now() ELSE NULL END,
                error=%s,
                source_manifest_sha256=CASE WHEN %s THEN %s ELSE source_manifest_sha256 END,
                answer_sha256=CASE WHEN %s THEN %s ELSE answer_sha256 END
                WHERE id=%s""",
                (
                    next_state,
                    next_revision,
                    reopen,
                    current_revision,
                    declared_outcome,
                    terminal,
                    error,
                    terminal,
                    completion.get("source_manifest_sha256"),
                    terminal,
                    completion.get("answer_sha256"),
                    run_id,
                ),
            )
            if reopen:
                cur.execute(
                    """UPDATE semantic_artifacts
                    SET validation_status='invalid',
                    validation_errors=validation_errors || %s::jsonb
                    WHERE run_id=%s AND validation_status='valid'""",
                    (
                        _canonical_json(
                            [
                                {
                                    "code": "stale_after_reopen",
                                    "invalidated_by_revision": next_revision,
                                    "reason": reason,
                                }
                            ]
                        ),
                        run_id,
                    ),
                )
                cur.execute(
                    """UPDATE research_runs SET source_manifest_sha256=NULL,
                    answer_sha256=NULL WHERE id=%s""",
                    (run_id,),
                )
            return {
                "transition_id": transition_id,
                "event_id": event_id,
                "lifecycle_revision": next_revision,
                "prior_state": prior_state,
                "next_state": next_state,
                "reused": False,
            }

    def revise_execution_mode(
        self,
        run_id,
        next_mode,
        expected_revision,
        idempotency_key,
        actor_type,
        policy_version,
        *,
        requested_by,
        approved_by,
        reason,
        actor_identifier=None,
    ):
        """Atomically revise semantic authority and append its approval event."""
        with self.__connection.cursor() as cur:
            state, current_revision = _lock_workflow_run(cur, run_id)
            cur.execute(
                "SELECT execution_mode FROM research_runs WHERE id=%s", (run_id,)
            )
            current_mode = cur.fetchone()[0]
            cur.execute(
                """SELECT id,run_revision,event_type,actor_type,actor_identifier,payload
                FROM research_events
                WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                expected_payload = {
                    "prior_mode": existing[5].get("prior_mode"),
                    "next_mode": next_mode,
                    "expected_revision": expected_revision,
                    "requested_by": requested_by,
                    "approved_by": approved_by,
                    "reason": reason,
                    "policy_version": policy_version,
                    "semantic_artifacts_invalidated": True,
                }
                if existing[2:] != (
                    "run.execution_mode_changed",
                    actor_type,
                    actor_identifier,
                    expected_payload,
                ):
                    raise ValueError("idempotency key was used for another mode change")
                return {
                    "event_id": existing[0],
                    "lifecycle_revision": existing[1],
                    "prior_mode": existing[5]["prior_mode"],
                    "next_mode": existing[5]["next_mode"],
                    "reused": True,
                }
            if current_revision != expected_revision:
                raise ValueError(
                    "stale research run revision: "
                    f"expected {expected_revision}, current {current_revision}"
                )
            if state in {"completed", "partial", "failed", "cancelled"}:
                raise ValueError(
                    "research run mode change rejected: terminal runs must be reopened"
                )
            if current_mode == next_mode:
                raise ValueError(
                    "research run mode change rejected: next mode equals current mode"
                )
            next_revision = current_revision + 1
            payload = {
                "prior_mode": current_mode,
                "next_mode": next_mode,
                "expected_revision": expected_revision,
                "requested_by": requested_by,
                "approved_by": approved_by,
                "reason": reason,
                "policy_version": policy_version,
                "semantic_artifacts_invalidated": True,
            }
            cur.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) FROM research_events WHERE run_id = %s",
                (run_id,),
            )
            next_seq = cur.fetchone()[0] + 1
            cur.execute(
                """INSERT INTO research_events(
                run_id,event_type,actor_type,actor_identifier,payload,
                run_revision,idempotency_key,sequence_number)
                VALUES(%s,'run.execution_mode_changed',%s,%s,%s,%s,%s,%s)
                RETURNING id""",
                (
                    run_id,
                    actor_type,
                    actor_identifier,
                    _canonical_json(payload),
                    next_revision,
                    idempotency_key,
                    next_seq,
                ),
            )
            event_id = cur.fetchone()[0]
            cur.execute(
                """UPDATE research_runs SET execution_mode=%s,lifecycle_revision=%s
                WHERE id=%s""",
                (next_mode, next_revision, run_id),
            )
            cur.execute(
                """UPDATE semantic_artifacts
                SET validation_status='invalid',
                validation_errors=validation_errors || %s::jsonb
                WHERE run_id=%s AND validation_status='valid'""",
                (
                    _canonical_json(
                        [
                            {
                                "code": "stale_after_mode_change",
                                "invalidated_by_revision": next_revision,
                                "prior_mode": current_mode,
                                "next_mode": next_mode,
                            }
                        ]
                    ),
                    run_id,
                ),
            )
            return {
                "event_id": event_id,
                "prior_mode": current_mode,
                "next_mode": next_mode,
                "lifecycle_revision": next_revision,
                "reused": False,
            }

    def record_invocation(
        self,
        run_id,
        operation,
        idempotency_key,
        *,
        parent_invocation_id=None,
        external_invocation_id=None,
        status="pending",
        input_payload=None,
        metadata=None,
    ):
        with self.__connection.cursor() as cur:
            _state, revision = _lock_workflow_run(cur, run_id)
            input_json = _canonical_json(input_payload or {})
            metadata_json = _canonical_json(metadata or {})
            cur.execute(
                """SELECT id,parent_invocation_id,external_invocation_id,operation,
                status,input,metadata FROM research_invocations
                WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                expected = (
                    parent_invocation_id,
                    external_invocation_id,
                    operation,
                    status,
                    json.loads(input_json),
                    json.loads(metadata_json),
                )
                if existing[1:] != expected:
                    raise ValueError("idempotency key was used for another invocation")
                return existing[0]
            cur.execute(
                """INSERT INTO research_invocations(
                run_id,parent_invocation_id,external_invocation_id,operation,status,
                lifecycle_revision,idempotency_key,input,metadata)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id""",
                (
                    run_id,
                    parent_invocation_id,
                    external_invocation_id,
                    operation,
                    status,
                    revision,
                    idempotency_key,
                    input_json,
                    metadata_json,
                ),
            )
            return cur.fetchone()[0]

    @staticmethod
    def _invocation_mapping(row):
        keys = (
            "id",
            "run_id",
            "parent_invocation_id",
            "external_invocation_id",
            "operation",
            "status",
            "lifecycle_revision",
            "input",
            "output",
            "error",
            "metadata",
            "started_at",
            "completed_at",
            "created_at",
        )
        return dict(zip(keys, row))

    def get_invocation_status(
        self,
        *,
        run_id=None,
        invocation_id=None,
        external_invocation_id=None,
    ):
        with self.__connection.cursor() as cur:
            columns = """id, run_id, parent_invocation_id, external_invocation_id,
                operation, status, lifecycle_revision, input, output,
                error, metadata, started_at, completed_at, created_at"""
            if invocation_id:
                cur.execute(
                    f"SELECT {columns} FROM research_invocations WHERE id = %s",
                    (invocation_id,),
                )
            elif external_invocation_id:
                cur.execute(
                    f"""SELECT {columns} FROM research_invocations
                    WHERE external_invocation_id = %s
                    ORDER BY created_at DESC LIMIT 1""",
                    (external_invocation_id,),
                )
            elif run_id:
                cur.execute(
                    f"""SELECT {columns} FROM research_invocations
                    WHERE run_id = %s ORDER BY created_at DESC LIMIT 1""",
                    (run_id,),
                )
            else:
                raise ValueError(
                    "must provide invocation_id, external_invocation_id, or run_id"
                )
            row = cur.fetchone()
            if row is None:
                raise KeyError("invocation not found")
            return self._invocation_mapping(row)

    def list_invocations(
        self,
        run_id,
        *,
        operation=None,
        status=None,
        limit=100,
        offset=0,
    ):
        columns = """id, run_id, parent_invocation_id, external_invocation_id,
            operation, status, lifecycle_revision, input, output,
            error, metadata, started_at, completed_at, created_at"""
        with self.__connection.cursor() as cur:
            if operation and status:
                cur.execute(
                    f"""SELECT {columns} FROM research_invocations
                    WHERE run_id = %s AND operation = %s AND status = %s
                    ORDER BY created_at ASC LIMIT %s OFFSET %s""",
                    (run_id, operation, status, limit, offset),
                )
            elif operation:
                cur.execute(
                    f"""SELECT {columns} FROM research_invocations
                    WHERE run_id = %s AND operation = %s
                    ORDER BY created_at ASC LIMIT %s OFFSET %s""",
                    (run_id, operation, limit, offset),
                )
            elif status:
                cur.execute(
                    f"""SELECT {columns} FROM research_invocations
                    WHERE run_id = %s AND status = %s
                    ORDER BY created_at ASC LIMIT %s OFFSET %s""",
                    (run_id, status, limit, offset),
                )
            else:
                cur.execute(
                    f"""SELECT {columns} FROM research_invocations
                    WHERE run_id = %s ORDER BY created_at ASC LIMIT %s OFFSET %s""",
                    (run_id, limit, offset),
                )
            return [self._invocation_mapping(row) for row in cur.fetchall()]

    def _bump_lifecycle_revision(self, run_id, new_revision, expected_revision=None):
        with self.__connection.cursor() as cur:
            if expected_revision is not None:
                cur.execute(
                    "UPDATE research_runs SET lifecycle_revision=%s WHERE id=%s AND lifecycle_revision=%s",
                    (new_revision, run_id, expected_revision),
                )
            else:
                cur.execute(
                    "UPDATE research_runs SET lifecycle_revision=%s WHERE id=%s",
                    (new_revision, run_id),
                )
            if expected_revision is not None and cur.rowcount == 0:
                from .run_service import RunStateError

                raise RunStateError(
                    f"Concurrency error: expected revision {expected_revision} for run {run_id}"
                )
            return cur.rowcount

    def append_event(
        self,
        run_id,
        event_type,
        actor_type,
        idempotency_key,
        *,
        invocation_id=None,
        actor_identifier=None,
        payload=None,
    ):
        with self.__connection.cursor() as cur:
            _state, revision = _lock_workflow_run(cur, run_id)
            payload_json = _canonical_json(payload or {})
            cur.execute(
                """SELECT id,invocation_id,event_type,actor_type,actor_identifier,payload,
                sequence_number FROM research_events
                WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                existing_payload = (
                    existing[5]
                    if isinstance(existing[5], dict)
                    else json.loads(existing[5])
                    if existing[5]
                    else {}
                )
                incoming_payload = json.loads(payload_json)
                if (
                    existing[1],
                    existing[2],
                    existing[3],
                    existing[4],
                    existing_payload,
                ) != (
                    invocation_id,
                    event_type,
                    actor_type,
                    actor_identifier,
                    incoming_payload,
                ):
                    raise ValueError("idempotency key was used for another event")
                return {"event_id": existing[0], "reused": True}
            cur.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) FROM research_events WHERE run_id = %s",
                (run_id,),
            )
            next_seq = cur.fetchone()[0] + 1
            cur.execute(
                """INSERT INTO research_events(
                run_id,invocation_id,event_type,actor_type,actor_identifier,payload,
                run_revision,idempotency_key,sequence_number)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id,event_type,run_revision,sequence_number""",
                (
                    run_id,
                    invocation_id,
                    event_type,
                    actor_type,
                    actor_identifier,
                    payload_json,
                    revision,
                    idempotency_key,
                    next_seq,
                ),
            )
            event_id, stored_type, stored_revision, _stored_seq = cur.fetchone()
            if (stored_type, stored_revision) != (event_type, revision):
                raise ValueError("idempotency key was used for another event")
            return event_id

    @staticmethod
    def _event_mapping(row):
        keys = (
            "id",
            "run_id",
            "invocation_id",
            "event_type",
            "actor_type",
            "actor_identifier",
            "payload",
            "sequence_number",
            "run_revision",
            "created_at",
        )
        return dict(zip(keys, row))

    def get_event_by_id(self, run_id, event_id):
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, invocation_id, event_type, actor_type,
                actor_identifier, payload, sequence_number, run_revision, created_at
                FROM research_events WHERE id = %s AND run_id = %s""",
                (event_id, run_id),
            )
            row = cur.fetchone()
            return None if row is None else self._event_mapping(row)

    def list_events(
        self,
        run_id,
        *,
        invocation_id=None,
        event_type=None,
        limit=100,
        offset=0,
    ):
        columns = """id, run_id, invocation_id, event_type, actor_type,
            actor_identifier, payload, sequence_number, run_revision, created_at"""
        with self.__connection.cursor() as cur:
            if invocation_id and event_type:
                cur.execute(
                    f"""SELECT {columns} FROM research_events
                    WHERE run_id = %s AND invocation_id = %s AND event_type = %s
                    ORDER BY sequence_number ASC LIMIT %s OFFSET %s""",
                    (run_id, invocation_id, event_type, limit, offset),
                )
            elif invocation_id:
                cur.execute(
                    f"""SELECT {columns} FROM research_events
                    WHERE run_id = %s AND invocation_id = %s
                    ORDER BY sequence_number ASC LIMIT %s OFFSET %s""",
                    (run_id, invocation_id, limit, offset),
                )
            elif event_type:
                cur.execute(
                    f"""SELECT {columns} FROM research_events
                    WHERE run_id = %s AND event_type = %s
                    ORDER BY sequence_number ASC LIMIT %s OFFSET %s""",
                    (run_id, event_type, limit, offset),
                )
            else:
                cur.execute(
                    f"""SELECT {columns} FROM research_events
                    WHERE run_id = %s ORDER BY sequence_number ASC LIMIT %s OFFSET %s""",
                    (run_id, limit, offset),
                )
            return [self._event_mapping(row) for row in cur.fetchall()]

    def next_event_sequence(self, run_id):
        with self.__connection.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) FROM research_events WHERE run_id = %s",
                (run_id,),
            )
            return cur.fetchone()[0] + 1

    def record_research_spec(
        self,
        run_id,
        spec_revision,
        schema_name,
        schema_version,
        payload,
        idempotency_key,
        *,
        validation_status="valid",
        validation_errors=None,
    ):
        with self.__connection.cursor() as cur:
            _lock_workflow_run(cur, run_id)
            digest = _json_sha256(payload)
            cur.execute(
                """INSERT INTO research_specs(
                run_id,spec_revision,schema_name,schema_version,payload,content_sha256,
                validation_status,validation_errors,idempotency_key)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(run_id,idempotency_key) DO UPDATE
                  SET idempotency_key=excluded.idempotency_key
                RETURNING id,spec_revision,content_sha256""",
                (
                    run_id,
                    spec_revision,
                    schema_name,
                    schema_version,
                    _canonical_json(payload),
                    digest,
                    validation_status,
                    _canonical_json(validation_errors or []),
                    idempotency_key,
                ),
            )
            spec_id, stored_revision, stored_digest = cur.fetchone()
            if (stored_revision, stored_digest) != (spec_revision, digest):
                raise ValueError("idempotency key was used for another research spec")
            cur.execute(
                "UPDATE research_runs SET research_spec_id=%s WHERE id=%s",
                (spec_id, run_id),
            )
            return spec_id

    def record_budget_snapshot(
        self,
        run_id,
        research_spec_id,
        spec_revision,
        run_revision,
        policy_version,
        policy_config_sha256,
        snapshot,
        idempotency_key,
    ):
        expected_snapshot_fields = {
            "policy_version": policy_version,
            "policy_config_sha256": policy_config_sha256,
            "spec_revision": spec_revision,
            "run_revision": run_revision,
        }
        mismatched = {
            name: {"expected": expected, "actual": snapshot.get(name)}
            for name, expected in expected_snapshot_fields.items()
            if snapshot.get(name) != expected
        }
        if mismatched:
            raise ValueError(
                f"budget snapshot envelope does not match repository arguments: {mismatched}"
            )
        with self.__connection.cursor() as cur:
            _, current_revision = _lock_workflow_run(cur, run_id)
            if run_revision != current_revision:
                raise ValueError(
                    f"budget snapshot run revision {run_revision} is stale; "
                    f"current revision is {current_revision}"
                )
            cur.execute(
                """SELECT spec_revision FROM research_specs
                WHERE id=%s AND run_id=%s""",
                (research_spec_id, run_id),
            )
            stored_spec = cur.fetchone()
            if stored_spec is None:
                raise ValueError("budget snapshot references an unknown research spec")
            if stored_spec[0] != spec_revision:
                raise ValueError(
                    "budget snapshot spec revision does not match its spec"
                )
            payload_json = _canonical_json(snapshot)
            digest = _json_sha256(snapshot)
            cur.execute(
                """SELECT id,research_spec_id,spec_revision,run_revision,
                policy_version,policy_config_sha256,content_sha256
                FROM research_budget_snapshots
                WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            idempotent = cur.fetchone()
            expected = (
                research_spec_id,
                spec_revision,
                run_revision,
                policy_version,
                policy_config_sha256,
                digest,
            )
            if idempotent is not None:
                if idempotent[1:] != expected:
                    raise ValueError(
                        "idempotency key was used for another budget snapshot"
                    )
                return idempotent[0]
            cur.execute(
                """SELECT id,research_spec_id,spec_revision,policy_config_sha256,
                content_sha256 FROM research_budget_snapshots
                WHERE run_id=%s AND policy_version=%s AND run_revision=%s""",
                (run_id, policy_version, run_revision),
            )
            existing = cur.fetchone()
            if existing is not None:
                expected = (
                    research_spec_id,
                    spec_revision,
                    policy_config_sha256,
                    digest,
                )
                if existing[1:] != expected:
                    raise ValueError(
                        "budget change requires a new policy version or explicit run revision"
                    )
                return existing[0]
            cur.execute(
                """INSERT INTO research_budget_snapshots(
                run_id,research_spec_id,spec_revision,run_revision,policy_version,
                policy_config_sha256,snapshot,content_sha256,idempotency_key)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(run_id,idempotency_key) DO UPDATE
                  SET idempotency_key=excluded.idempotency_key
                RETURNING id,research_spec_id,spec_revision,run_revision,policy_version,
                policy_config_sha256,content_sha256""",
                (
                    run_id,
                    research_spec_id,
                    spec_revision,
                    run_revision,
                    policy_version,
                    policy_config_sha256,
                    payload_json,
                    digest,
                    idempotency_key,
                ),
            )
            row = cur.fetchone()
            expected = (
                research_spec_id,
                spec_revision,
                run_revision,
                policy_version,
                policy_config_sha256,
                digest,
            )
            if row[1:] != expected:
                raise ValueError("idempotency key was used for another budget snapshot")
            cur.execute(
                """UPDATE research_runs SET budget_snapshot_id=%s,
                budget_policy_version=%s WHERE id=%s""",
                (row[0], policy_version, run_id),
            )
            return row[0]

    def get_research_spec(self, run_id):
        """Return the latest research spec payload for one run, or ``None``."""
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, spec_revision, payload
                FROM research_specs WHERE run_id=%s
                ORDER BY spec_revision DESC LIMIT 1""",
                (str(run_id),),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": str(row[0]),
            "run_id": str(row[1]),
            "spec_revision": row[2],
            "payload": row[3],
        }

    def get_latest_budget_snapshot(self, run_id):
        """Return the latest budget snapshot for one run, or ``None``."""
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id,research_spec_id,spec_revision,run_revision,
                          policy_version,policy_config_sha256,snapshot
                   FROM research_budget_snapshots
                   WHERE run_id=%s
                   ORDER BY run_revision DESC,created_at DESC,id DESC
                   LIMIT 1""",
                (run_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "research_spec_id": row[1],
            "spec_revision": int(row[2]),
            "run_revision": int(row[3]),
            "policy_version": str(row[4]),
            "policy_config_sha256": str(row[5]),
            "snapshot": row[6],
        }
