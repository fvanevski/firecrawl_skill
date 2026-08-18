"""Internal mixin for durable PostgreSQL indexing checkpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from ...asset_promotion_service import AssetPromotionService
from ...index_census import census_index_jobs
from .index_checkpoint_models import (
    IndexCheckpoint,
    IndexCheckpointError,
    IndexCheckpointStaleError,
    _membership_digest,
)


class _IndexCheckpointCoreMixin:
    def __init__(self, uow_factory, *, max_attempts: int = 5):
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.uow_factory = uow_factory
        self.max_attempts = max_attempts
        self.asset_promotions = AssetPromotionService(uow_factory)

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
        base_key = idempotency_key or (
            f"index-checkpoint:{run_id}:r{lifecycle_revision}:{fingerprint}"
        )

        prepared_asset_seal = self._prepare_asset_membership_if_needed(
            run_id, lifecycle_revision
        )
        key = (
            base_key
            if prepared_asset_seal is None
            else f"{base_key}:asset-seal:{prepared_asset_seal.seal_revision}"
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

            if prepared_asset_seal is None:
                raise IndexCheckpointError(
                    "new checkpoint creation requires sealed completion membership"
                )
            entity_ids = prepared_asset_seal.chunk_ids
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

    @classmethod
    def checkpoint_census(cls, checkpoint: IndexCheckpoint) -> dict[str, Any]:
        """Return the latest persisted census in the public checkpoint schema."""
        return cls._checkpoint_census(checkpoint)

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
