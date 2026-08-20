"""Guarded finalization for durable PostgreSQL indexing checkpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from ...asset_promotion_service import AssetMembershipSeal, AssetPromotionService
from ...index_census import census_index_jobs
from .index_checkpoint_models import (
    IRRECOVERABLE_CLASSES,
    RECOVERABLE_CLASSES,
    IndexCheckpoint,
    IndexCheckpointStaleError,
    IndexFinalization,
    _membership_digest,
)


class _IndexCheckpointFinalizeMixin:
    if TYPE_CHECKING:
        uow_factory: Any
        max_attempts: int
        asset_promotions: AssetPromotionService
        def _by_id(self, cursor: Any, checkpoint_id: UUID, *, for_update: bool = False) -> IndexCheckpoint: ...
        def _invalidate(self, cursor: Any, checkpoint: IndexCheckpoint, reason: str) -> IndexCheckpoint: ...
        def _current_membership(self, uow: Any, cursor: Any, run_id: UUID) -> tuple[UUID, ...]: ...
        def _validate_census(self, checkpoint: IndexCheckpoint, census: dict[str, Any]) -> None: ...
        def _manifest_count(self, cursor: Any, entity_ids: tuple[UUID, ...], fingerprint: str) -> int: ...
        def _write_observation(self, cursor: Any, checkpoint: IndexCheckpoint, census: dict[str, Any], *, manifest_count: int, deadline_at: Any = None) -> IndexCheckpoint: ...
        @staticmethod
        def _checkpoint_census(checkpoint: IndexCheckpoint) -> dict[str, Any]: ...
        @staticmethod
        def _validate_asset_binding(cursor: Any, checkpoint_id: UUID, asset_seal: AssetMembershipSeal) -> None: ...
        @staticmethod
        def _definition_count(cursor: Any, fingerprint: str) -> int: ...
        @staticmethod
        def _completion_payload(checkpoint: IndexCheckpoint, census: dict[str, Any], *, manifest_count: int, asset_seal: AssetMembershipSeal | None) -> dict[str, Any]: ...

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
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            state, current_revision = uow.runs._lock_workflow_run(cursor, run_id)
            checkpoint = self._by_id(cursor, checkpoint_id, for_update=True)
            if checkpoint.run_id != run_id:
                raise IndexCheckpointStaleError("checkpoint belongs to another run")
            if checkpoint.status == "completed":
                persisted_census = self._checkpoint_census(checkpoint)
                asset_seal = self.asset_promotions.load_active_seal_in_transaction(cursor, run_id)
                transition = uow.runs.apply_run_transition(
                    run_id, "coverage_review", expected_revision, idempotency_key,
                    actor_type, "run-state-v1", permitted_prior_states=frozenset({"indexing"}),
                    actor_identifier=actor_identifier, event_type="run.indexing_checkpoint_completed",
                    reason=reason,
                    completion=self._completion_payload(
                        checkpoint, persisted_census,
                        manifest_count=checkpoint.manifest_count, asset_seal=asset_seal,
                    ),
                )
                return IndexFinalization("reused", checkpoint, persisted_census, int(transition["lifecycle_revision"]), transition=transition)
            if state != "indexing":
                invalidated = self._invalidate(cursor, checkpoint, "run_left_indexing")
                return IndexFinalization("invalidated", invalidated, self._checkpoint_census(invalidated), int(current_revision), reason=f"run state changed to {state}")
            if int(current_revision) != expected_revision:
                invalidated = self._invalidate(cursor, checkpoint, "lifecycle_revision_changed")
                return IndexFinalization("invalidated", invalidated, self._checkpoint_census(invalidated), int(current_revision), reason=f"expected revision {expected_revision}, current {current_revision}")
            if checkpoint.lifecycle_revision != int(current_revision):
                invalidated = self._invalidate(cursor, checkpoint, "checkpoint_revision_changed")
                return IndexFinalization("invalidated", invalidated, self._checkpoint_census(invalidated), int(current_revision), reason="checkpoint was sealed at another lifecycle revision")
            asset_seal = self.asset_promotions.load_active_seal_in_transaction(cursor, run_id, for_update=True)
            current_membership = asset_seal.chunk_ids if asset_seal is not None else self._current_membership(uow, cursor, run_id)
            if _membership_digest(current_membership) != checkpoint.expected_membership_sha256 or current_membership != checkpoint.entity_ids:
                invalidated = self._invalidate(cursor, checkpoint, "membership_changed")
                return IndexFinalization("invalidated", invalidated, self._checkpoint_census(invalidated), int(current_revision), reason="sealed completion membership changed")
            if asset_seal is not None:
                self._validate_asset_binding(cursor, checkpoint.id, asset_seal)
            definition_count = self._definition_count(cursor, checkpoint.fingerprint)
            if definition_count != 1:
                invalidated = self._invalidate(cursor, checkpoint, "index_definition_changed")
                return IndexFinalization("invalidated", invalidated, self._checkpoint_census(invalidated), int(current_revision), reason=f"active fingerprint resolved to {definition_count} definitions")
            census = census_index_jobs(uow.connection, checkpoint.entity_ids, checkpoint.fingerprint, max_attempts=self.max_attempts)
            self._validate_census(checkpoint, census)
            manifest_count = self._manifest_count(cursor, checkpoint.entity_ids, checkpoint.fingerprint)
            checkpoint = self._write_observation(cursor, checkpoint, census, manifest_count=manifest_count)
            irrecoverable = sum(int(census[name]) for name in IRRECOVERABLE_CLASSES)
            recoverable = sum(int(census[name]) for name in RECOVERABLE_CLASSES)
            expected = int(census["expected"])
            complete = int(census["complete"])
            if irrecoverable:
                return IndexFinalization("irrecoverable", checkpoint, census, int(current_revision), reason="irrecoverable census classes remain")
            if recoverable or complete != expected or manifest_count != expected:
                return IndexFinalization("recoverable", checkpoint, census, int(current_revision), reason="indexing remains incomplete and resumable")
            persisted_census = self._checkpoint_census(checkpoint)
            transition = uow.runs.apply_run_transition(
                run_id, "coverage_review", expected_revision, idempotency_key,
                actor_type, "run-state-v1", permitted_prior_states=frozenset({"indexing"}),
                actor_identifier=actor_identifier, event_type="run.indexing_checkpoint_completed",
                reason=reason,
                completion=self._completion_payload(
                    checkpoint, persisted_census, manifest_count=manifest_count, asset_seal=asset_seal
                ),
            )
            cursor.execute(
                """UPDATE indexing_checkpoints SET status='completed', completed_at=now(), updated_at=now() WHERE id=%s""",
                (checkpoint.id,),
            )
            checkpoint = self._by_id(cursor, checkpoint.id, for_update=True)
            return IndexFinalization("advanced", checkpoint, census, int(transition["lifecycle_revision"]), transition=transition)
