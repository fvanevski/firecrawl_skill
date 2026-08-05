"""Durable PostgreSQL indexing checkpoints and guarded finalization."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from .index_census import census_index_jobs
from .index_checkpoint_models import (
    IRRECOVERABLE_CLASSES,
    RECOVERABLE_CLASSES,
    IndexCheckpoint,
    IndexCheckpointError,
    IndexCheckpointStaleError,
    IndexFinalization,
    _membership_digest,
)
from .index_checkpoint_store import IndexCheckpointStoreMixin


class IndexCheckpointService(IndexCheckpointStoreMixin):
    """Seal, observe, resume, and atomically finalize one run's index set."""

    def __init__(self, uow_factory, *, max_attempts: int = 5):
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.uow_factory = uow_factory
        self.max_attempts = max_attempts

    def active_fingerprint(self, run_id: UUID) -> str:
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT definition.fingerprint
                     FROM index_definitions definition
                    WHERE definition.physical_collection=%s
                    ORDER BY definition.created_at DESC, definition.id DESC""",
                (uow.index_name,),
            )
            rows = cursor.fetchall()
        fingerprints = {str(row[0]) for row in rows}
        if len(fingerprints) != 1:
            raise IndexCheckpointError(
                f"expected one configured index fingerprint for run {run_id}, "
                f"found {len(fingerprints)}"
            )
        return fingerprints.pop()

    def ensure(
        self,
        run_id: UUID,
        *,
        lifecycle_revision: int,
        fingerprint: str,
        deadline_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> IndexCheckpoint:
        if lifecycle_revision < 0:
            raise ValueError("lifecycle_revision must be non-negative")
        fingerprint = fingerprint.strip()
        if not fingerprint:
            raise ValueError("fingerprint is required")
        key = idempotency_key or (
            f"index-checkpoint:{run_id}:r{lifecycle_revision}:{fingerprint}"
        )

        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            state, current_revision = uow.runs._lock_workflow_run(cursor, run_id)
            if state != "indexing":
                completed = self._latest(cursor, run_id, statuses=("completed",))
                if completed is not None:
                    return completed
                raise IndexCheckpointStaleError(
                    f"run {run_id} must be indexing to create a checkpoint; got {state}"
                )
            if int(current_revision) != lifecycle_revision:
                raise IndexCheckpointStaleError(
                    "checkpoint revision is stale: "
                    f"expected {lifecycle_revision}, current {current_revision}"
                )

            active = self._latest(cursor, run_id, statuses=("active",), for_update=True)
            if active is not None:
                if active.lifecycle_revision != lifecycle_revision:
                    return self._invalidate(
                        cursor,
                        active,
                        "lifecycle_revision_changed",
                    )
                if active.fingerprint != fingerprint:
                    return self._invalidate(cursor, active, "index_fingerprint_changed")
                if (
                    _membership_digest(active.entity_ids)
                    != active.expected_membership_sha256
                ):
                    return self._invalidate(
                        cursor, active, "stored_membership_hash_mismatch"
                    )
                if deadline_at is not None:
                    cursor.execute(
                        """UPDATE indexing_checkpoints
                              SET deadline_at=COALESCE(deadline_at,%s), updated_at=now()
                            WHERE id=%s""",
                        (deadline_at, active.id),
                    )
                    active = self._by_id(cursor, active.id, for_update=True)
                return active

            entity_ids = self._current_membership(uow, cursor, run_id)
            if not entity_ids:
                raise IndexCheckpointError(
                    f"run {run_id} has no exact PostgreSQL chunk membership to index"
                )
            digest = _membership_digest(entity_ids)
            cursor.execute(
                """INSERT INTO indexing_checkpoints(
                       run_id,lifecycle_revision,fingerprint,entity_ids,
                       expected_membership_sha256,expected_count,deadline_at,
                       idempotency_key)
                     VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                     ON CONFLICT(run_id,idempotency_key) DO UPDATE
                       SET idempotency_key=excluded.idempotency_key
                     RETURNING id""",
                (
                    run_id,
                    lifecycle_revision,
                    fingerprint,
                    list(entity_ids),
                    digest,
                    len(entity_ids),
                    deadline_at,
                    key,
                ),
            )
            checkpoint_id = cursor.fetchone()[0]
            checkpoint = self._by_id(cursor, checkpoint_id, for_update=True)
            census = census_index_jobs(
                uow.connection,
                checkpoint.entity_ids,
                checkpoint.fingerprint,
                max_attempts=self.max_attempts,
            )
            manifest_count = self._manifest_count(
                cursor, checkpoint.entity_ids, checkpoint.fingerprint
            )
            return self._write_observation(
                cursor,
                checkpoint,
                census,
                manifest_count=manifest_count,
                deadline_at=deadline_at,
            )

    def get_active(self, run_id: UUID) -> IndexCheckpoint | None:
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            return self._latest(cursor, run_id, statuses=("active",))

    def latest_for_terminal(self, run_id: UUID) -> IndexCheckpoint | None:
        """Return the newest durable checkpoint usable as terminal evidence."""

        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            return self._latest(
                cursor,
                run_id,
                statuses=("completed", "active", "invalidated"),
            )

    @staticmethod
    def checkpoint_census(checkpoint: IndexCheckpoint) -> dict[str, Any]:
        """Return the latest persisted census in the public checkpoint schema."""
        return IndexCheckpointService._checkpoint_census(checkpoint)

    def observe(
        self,
        checkpoint_id: UUID,
        census: dict[str, Any],
        *,
        deadline_at: datetime | None = None,
    ) -> IndexCheckpoint:
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            checkpoint = self._by_id(cursor, checkpoint_id, for_update=True)
            self._validate_census(checkpoint, census)
            manifest_count = self._manifest_count(
                cursor, checkpoint.entity_ids, checkpoint.fingerprint
            )
            return self._write_observation(
                cursor,
                checkpoint,
                census,
                manifest_count=manifest_count,
                deadline_at=deadline_at,
            )

    def finalize(
        self,
        run_id: UUID,
        checkpoint_id: UUID,
        *,
        expected_revision: int,
        idempotency_key: str,
        actor_type: str = "wrapper",
        actor_identifier: str | None = "firecrawl-skill",
        reason: str = "sealed indexing checkpoint is complete",
    ) -> IndexFinalization:
        """Fresh-read the sealed set and CAS indexing -> coverage_review.

        The run row is locked first, serializing concurrent finish/resume calls.
        Membership, lifecycle revision, fingerprint, manifest count, and census
        are then re-read inside this fresh transaction. The transition and
        checkpoint completion update commit atomically.
        """

        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            state, current_revision = uow.runs._lock_workflow_run(cursor, run_id)
            checkpoint = self._by_id(cursor, checkpoint_id, for_update=True)
            if checkpoint.run_id != run_id:
                raise IndexCheckpointStaleError("checkpoint belongs to another run")

            if checkpoint.status == "completed":
                persisted_census = self._checkpoint_census(checkpoint)
                transition = uow.runs.apply_run_transition(
                    run_id,
                    "coverage_review",
                    expected_revision,
                    idempotency_key,
                    actor_type,
                    "run-state-v1",
                    permitted_prior_states=frozenset({"indexing"}),
                    actor_identifier=actor_identifier,
                    event_type="run.indexing_checkpoint_completed",
                    reason=reason,
                    completion={
                        "indexing_checkpoint_id": str(checkpoint.id),
                        "membership_sha256": checkpoint.expected_membership_sha256,
                        "fingerprint": checkpoint.fingerprint,
                        "expected": checkpoint.expected_count,
                        "complete": checkpoint.complete_count,
                        "manifest_count": checkpoint.manifest_count,
                        "census": persisted_census,
                    },
                )
                return IndexFinalization(
                    "reused",
                    checkpoint,
                    persisted_census,
                    int(transition["lifecycle_revision"]),
                    transition=transition,
                )

            if state != "indexing":
                invalidated = self._invalidate(cursor, checkpoint, "run_left_indexing")
                return IndexFinalization(
                    "invalidated",
                    invalidated,
                    self._checkpoint_census(invalidated),
                    int(current_revision),
                    reason=f"run state changed to {state}",
                )
            if int(current_revision) != expected_revision:
                invalidated = self._invalidate(
                    cursor, checkpoint, "lifecycle_revision_changed"
                )
                return IndexFinalization(
                    "invalidated",
                    invalidated,
                    self._checkpoint_census(invalidated),
                    int(current_revision),
                    reason=(
                        f"expected revision {expected_revision}, "
                        f"current {current_revision}"
                    ),
                )
            if checkpoint.lifecycle_revision != int(current_revision):
                invalidated = self._invalidate(
                    cursor, checkpoint, "checkpoint_revision_changed"
                )
                return IndexFinalization(
                    "invalidated",
                    invalidated,
                    self._checkpoint_census(invalidated),
                    int(current_revision),
                    reason="checkpoint was sealed at another lifecycle revision",
                )

            current_membership = self._current_membership(uow, cursor, run_id)
            current_digest = _membership_digest(current_membership)
            if (
                current_digest != checkpoint.expected_membership_sha256
                or current_membership != checkpoint.entity_ids
            ):
                invalidated = self._invalidate(cursor, checkpoint, "membership_changed")
                return IndexFinalization(
                    "invalidated",
                    invalidated,
                    self._checkpoint_census(invalidated),
                    int(current_revision),
                    reason="sealed run membership changed",
                )

            definition_count = self._definition_count(cursor, checkpoint.fingerprint)
            if definition_count != 1:
                invalidated = self._invalidate(
                    cursor, checkpoint, "index_definition_changed"
                )
                return IndexFinalization(
                    "invalidated",
                    invalidated,
                    self._checkpoint_census(invalidated),
                    int(current_revision),
                    reason=(
                        f"active fingerprint resolved to {definition_count} definitions"
                    ),
                )

            census = census_index_jobs(
                uow.connection,
                checkpoint.entity_ids,
                checkpoint.fingerprint,
                max_attempts=self.max_attempts,
            )
            self._validate_census(checkpoint, census)
            manifest_count = self._manifest_count(
                cursor, checkpoint.entity_ids, checkpoint.fingerprint
            )
            checkpoint = self._write_observation(
                cursor,
                checkpoint,
                census,
                manifest_count=manifest_count,
            )

            irrecoverable = sum(int(census[name]) for name in IRRECOVERABLE_CLASSES)
            recoverable = sum(int(census[name]) for name in RECOVERABLE_CLASSES)
            expected = int(census["expected"])
            complete = int(census["complete"])
            if irrecoverable:
                return IndexFinalization(
                    "irrecoverable",
                    checkpoint,
                    census,
                    int(current_revision),
                    reason="irrecoverable census classes remain",
                )
            if recoverable or complete != expected or manifest_count != expected:
                return IndexFinalization(
                    "recoverable",
                    checkpoint,
                    census,
                    int(current_revision),
                    reason="indexing remains incomplete and resumable",
                )

            persisted_census = self._checkpoint_census(checkpoint)
            transition = uow.runs.apply_run_transition(
                run_id,
                "coverage_review",
                expected_revision,
                idempotency_key,
                actor_type,
                "run-state-v1",
                permitted_prior_states=frozenset({"indexing"}),
                actor_identifier=actor_identifier,
                event_type="run.indexing_checkpoint_completed",
                reason=reason,
                completion={
                    "indexing_checkpoint_id": str(checkpoint.id),
                    "membership_sha256": checkpoint.expected_membership_sha256,
                    "fingerprint": checkpoint.fingerprint,
                    "expected": expected,
                    "complete": complete,
                    "manifest_count": manifest_count,
                    "census": persisted_census,
                },
            )
            cursor.execute(
                """UPDATE indexing_checkpoints
                      SET status='completed', completed_at=now(), updated_at=now()
                    WHERE id=%s""",
                (checkpoint.id,),
            )
            checkpoint = self._by_id(cursor, checkpoint.id, for_update=True)
            return IndexFinalization(
                "advanced",
                checkpoint,
                census,
                int(transition["lifecycle_revision"]),
                transition=transition,
            )
