"""Read-only verification and replay of one completed indexing checkpoint."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .index_census import census_index_jobs
from .index_checkpoint_models import (
    IRRECOVERABLE_CLASSES,
    RECOVERABLE_CLASSES,
    IndexCheckpointError,
    IndexFinalization,
    _membership_digest,
)
from .index_checkpoint_service import IndexCheckpointService


def replay_completed_checkpoint(
    service: IndexCheckpointService,
    run_id: UUID,
) -> IndexFinalization:
    """Verify and replay the committed ``indexing -> coverage_review`` result.

    This path is deliberately read-only. It accepts only a run that remains at
    the exact lifecycle revision produced by the completed checkpoint, verifies
    current PostgreSQL membership, index definition, manifest count, and job
    census, and checks that the immutable transition payload is identical to the
    persisted checkpoint. It never calls ``apply_run_transition`` and therefore
    cannot create a second transition.
    """

    with service.uow_factory() as uow, uow.connection.cursor() as cursor:
        state, current_revision = uow.runs._lock_workflow_run(cursor, run_id)
        if state != "coverage_review":
            raise IndexCheckpointError(
                "completed checkpoint replay requires unchanged coverage_review "
                f"state; got {state}"
            )

        checkpoint = service._latest(
            cursor,
            run_id,
            statuses=("completed",),
            for_update=True,
        )
        if checkpoint is None:
            raise IndexCheckpointError(
                f"run {run_id} has no completed indexing checkpoint to replay"
            )
        if int(current_revision) != checkpoint.lifecycle_revision + 1:
            raise IndexCheckpointError(
                "completed checkpoint replay is stale: "
                f"checkpoint revision {checkpoint.lifecycle_revision}, "
                f"current revision {current_revision}"
            )

        asset_seal = service.asset_promotions.load_active_seal_in_transaction(
            cursor,
            run_id,
        )
        current_membership = (
            asset_seal.chunk_ids
            if asset_seal is not None
            else service._current_membership(uow, cursor, run_id)
        )
        if (
            current_membership != checkpoint.entity_ids
            or _membership_digest(current_membership)
            != checkpoint.expected_membership_sha256
        ):
            raise IndexCheckpointError(
                "completed checkpoint replay failed: sealed membership changed"
            )
        if asset_seal is not None:
            service._validate_asset_binding(cursor, checkpoint.id, asset_seal)
        if service._definition_count(cursor, checkpoint.fingerprint) != 1:
            raise IndexCheckpointError(
                "completed checkpoint replay failed: index definition changed"
            )

        census = census_index_jobs(
            uow.connection,
            checkpoint.entity_ids,
            checkpoint.fingerprint,
            max_attempts=service.max_attempts,
        )
        service._validate_census(checkpoint, census)
        manifest_count = service._manifest_count(
            cursor,
            checkpoint.entity_ids,
            checkpoint.fingerprint,
        )
        irrecoverable = sum(int(census[name]) for name in IRRECOVERABLE_CLASSES)
        recoverable = sum(int(census[name]) for name in RECOVERABLE_CLASSES)
        expected = checkpoint.expected_count
        if (
            irrecoverable
            or recoverable
            or int(census["complete"]) != expected
            or manifest_count != expected
        ):
            raise IndexCheckpointError(
                "completed checkpoint replay failed: authoritative census no "
                "longer proves exact completion"
            )

        persisted_census = service._checkpoint_census(checkpoint)
        expected_completion: dict[str, Any] = service._completion_payload(
            checkpoint,
            persisted_census,
            manifest_count=checkpoint.manifest_count,
            asset_seal=asset_seal,
        )
        cursor.execute(
            """SELECT id,triggering_event_id,lifecycle_revision,prior_state,
                      next_state,validation_result,idempotency_key
                 FROM research_run_transitions
                WHERE run_id=%s AND lifecycle_revision=%s
                  AND prior_state='indexing' AND next_state='coverage_review'""",
            (run_id, current_revision),
        )
        row = cursor.fetchone()
        if row is None:
            raise IndexCheckpointError(
                "completed checkpoint replay failed: matching lifecycle "
                "transition is missing"
            )
        validation_result = row[5] or {}
        if not isinstance(validation_result, dict):
            raise IndexCheckpointError(
                "completed checkpoint replay failed: transition validation "
                "payload is malformed"
            )
        if validation_result.get("completion") != expected_completion:
            raise IndexCheckpointError(
                "completed checkpoint replay failed: transition payload does "
                "not match the persisted checkpoint"
            )

        transition = {
            "transition_id": row[0],
            "event_id": row[1],
            "lifecycle_revision": int(row[2]),
            "prior_state": str(row[3]),
            "next_state": str(row[4]),
            "idempotency_key": str(row[6]),
            "reused": True,
        }
        return IndexFinalization(
            "reused",
            checkpoint,
            persisted_census,
            int(current_revision),
            transition=transition,
            reason="completed checkpoint and immutable transition replayed",
        )
