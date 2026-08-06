"""Direct-wrapper invocation persistence with exact lifecycle provenance."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from .invocation_events import _sanitize
from .invocation_service import (
    InvocationAlreadyRunning,
    InvocationError,
    InvocationRecord,
    InvocationService,
)


class DirectInvocationService(InvocationService):
    """Persist a direct invocation and its start-state in one transaction.

    The run row is held with ``FOR SHARE`` until the invocation and start event
    commit. A concurrent lifecycle transition therefore cannot interleave
    between the state observation and the invocation insert.
    """

    def begin(
        self,
        run_id: UUID,
        external_invocation_id: str,
        operation: str,
        input_data: dict[str, Any],
        *,
        parent_invocation_id: UUID | None = None,
        idempotency_key: str | None = None,
        actor_type: str = "system",
    ) -> InvocationRecord:
        if not run_id:
            raise ValueError("run_id is required")
        if not external_invocation_id.strip():
            raise ValueError("external_invocation_id is required")
        if not operation.strip():
            raise ValueError("operation is required")

        sanitized_input = _sanitize(input_data)
        command_key = idempotency_key or f"invocation:begin:{external_invocation_id}"

        with self.uow_factory() as uow:
            cur = uow.connection.cursor()
            cur.execute(
                """SELECT id,run_id,operation,status,input
                   FROM research_invocations
                   WHERE external_invocation_id=%s""",
                (external_invocation_id,),
            )
            existing = cur.fetchone()
            if existing:
                (
                    invocation_id,
                    existing_run_id,
                    existing_operation,
                    status,
                    existing_input,
                ) = existing
                if (
                    existing_run_id == run_id
                    and existing_operation == operation
                    and existing_input == sanitized_input
                    and status == "running"
                ):
                    return InvocationRecord.from_mapping(
                        uow.runs.get_invocation_status(invocation_id=invocation_id)
                    )
                if status == "running":
                    raise InvocationAlreadyRunning(
                        f"invocation {external_invocation_id} is already running"
                    )
                raise InvocationError(
                    f"invocation ID {external_invocation_id} is already terminal; "
                    "use a new ID"
                )

            cur.execute(
                """SELECT state,lifecycle_revision
                   FROM research_runs WHERE id=%s FOR SHARE""",
                (run_id,),
            )
            run_row = cur.fetchone()
            if run_row is None:
                raise KeyError(f"run {run_id} not found")
            lifecycle_state = str(run_row[0])
            lifecycle_revision = int(run_row[1])
            metadata = {
                "actor_type": actor_type,
                "lifecycle_state": lifecycle_state,
                "lifecycle_revision": lifecycle_revision,
            }

            cur.execute(
                """INSERT INTO research_invocations(
                run_id,parent_invocation_id,external_invocation_id,
                operation,status,lifecycle_revision,idempotency_key,
                input,metadata,started_at)
                VALUES(%s,%s,%s,%s,'running',%s,%s,%s,%s,now())
                RETURNING id""",
                (
                    run_id,
                    parent_invocation_id,
                    external_invocation_id,
                    operation,
                    lifecycle_revision,
                    command_key,
                    json.dumps(sanitized_input),
                    json.dumps(metadata),
                ),
            )
            invocation_id = cur.fetchone()[0]
            uow.runs.append_event(
                run_id,
                "invocation_started",
                actor_type,
                f"invocation:started:{external_invocation_id}",
                invocation_id=invocation_id,
                payload={
                    "operation": operation,
                    "external_invocation_id": external_invocation_id,
                    "lifecycle_state": lifecycle_state,
                    "lifecycle_revision": lifecycle_revision,
                },
            )
            return InvocationRecord.from_mapping(
                uow.runs.get_invocation_status(invocation_id=invocation_id)
            )
