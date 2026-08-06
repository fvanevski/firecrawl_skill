"""Internal mixin for PostgreSQL-authoritative asset promotion."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .asset_promotion_models import (
    AssetMembershipSeal,
    AssetMembershipSealedError,
    AssetPromotionError,
)

DEFAULT_POLICY_VERSION = "completion-membership-v1"


class _AssetPromotionCoreMixin:
    def list_assets(self, run_id: UUID) -> list[dict[str, Any]]:
        """Return authoritative subjects plus honest historical compatibility rows."""
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT id,candidate_id,snapshot_id,role,current_stage,
                          stage_revision,provenance,actor_type,actor_identifier,
                          policy_version,lifecycle_revision,reason_code,reason,
                          created_at,updated_at
                     FROM run_asset_promotion_subjects
                    WHERE run_id=%s
                    ORDER BY created_at,id""",
                (run_id,),
            )
            result = [self._subject_dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """SELECT asset.snapshot_id,asset.role,asset.created_at
                     FROM research_run_assets asset
                     LEFT JOIN run_asset_promotion_subjects subject
                       ON subject.run_id=asset.run_id
                      AND subject.snapshot_id=asset.snapshot_id
                      AND subject.role=asset.role
                    WHERE asset.run_id=%s AND subject.id IS NULL
                    ORDER BY asset.created_at,asset.snapshot_id,asset.role""",
                (run_id,),
            )
            for snapshot_id, role, created_at in cursor.fetchall():
                result.append(
                    {
                        "id": None,
                        "candidate_id": None,
                        "snapshot_id": str(snapshot_id),
                        "role": str(role),
                        "current_stage": "unknown",
                        "stage_revision": None,
                        "provenance": "legacy_unstructured",
                        "actor_type": None,
                        "actor_identifier": None,
                        "policy_version": None,
                        "lifecycle_revision": None,
                        "reason_code": "historical_stage_unknown",
                        "reason": (
                            "The run asset predates staged promotion; no history "
                            "was inferred"
                        ),
                        "created_at": created_at,
                        "updated_at": created_at,
                    }
                )
            return result

    def list_events(self, run_id: UUID) -> list[dict[str, Any]]:
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT id,subject_id,from_stage,to_stage,stage_revision,
                          actor_type,actor_identifier,policy_version,
                          lifecycle_revision,reason_code,reason,occurred_at,
                          transaction_id::text
                     FROM run_asset_promotion_events
                    WHERE run_id=%s
                    ORDER BY occurred_at,id""",
                (run_id,),
            )
            names = (
                "id",
                "subject_id",
                "from_stage",
                "to_stage",
                "stage_revision",
                "actor_type",
                "actor_identifier",
                "policy_version",
                "lifecycle_revision",
                "reason_code",
                "reason",
                "occurred_at",
                "transaction_id",
            )
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def promote(
        self,
        subject_id: UUID,
        target_stage: str,
        *,
        expected_lifecycle_revision: int,
        expected_run_id: UUID | None = None,
        actor_type: str,
        actor_identifier: str | None,
        policy_version: str,
        reason_code: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Apply one legal stage transition with lifecycle CAS metadata."""
        self._require_text(actor_type, "actor_type")
        self._require_text(policy_version, "policy_version")
        self._require_text(reason_code, "reason_code")
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            cursor.execute(
                "SELECT run_id FROM run_asset_promotion_subjects WHERE id=%s",
                (subject_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(subject_id)
            run_id = UUID(str(row[0]))
            if expected_run_id is not None and run_id != expected_run_id:
                raise AssetPromotionError(
                    f"asset promotion subject {subject_id} belongs to run {run_id}, "
                    f"not requested run {expected_run_id}"
                )
            self._lock_run(
                uow,
                cursor,
                run_id,
                expected_lifecycle_revision,
                require_indexing=False,
            )
            cursor.execute(
                """SELECT current_stage FROM run_asset_promotion_subjects
                    WHERE id=%s AND run_id=%s FOR UPDATE""",
                (subject_id, run_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(subject_id)
            current_stage = str(row[0])
            if current_stage == target_stage:
                return self._subject_by_id(cursor, subject_id)
            if (
                current_stage == "completion_critical"
                or target_stage == "completion_critical"
            ):
                cursor.execute(
                    """SELECT 1 FROM run_asset_membership_seals
                        WHERE run_id=%s AND status='sealed'""",
                    (run_id,),
                )
                if cursor.fetchone() is not None:
                    raise AssetMembershipSealedError(
                        "completion membership is sealed; reopen it before "
                        "changing membership"
                    )
            cursor.execute(
                """UPDATE run_asset_promotion_subjects
                      SET current_stage=%s,actor_type=%s,actor_identifier=%s,
                          policy_version=%s,lifecycle_revision=%s,
                          reason_code=%s,reason=%s
                    WHERE id=%s
                    RETURNING id,candidate_id,snapshot_id,role,current_stage,
                              stage_revision,provenance,actor_type,actor_identifier,
                              policy_version,lifecycle_revision,reason_code,reason,
                              created_at,updated_at""",
                (
                    target_stage,
                    actor_type,
                    actor_identifier,
                    policy_version,
                    expected_lifecycle_revision,
                    reason_code,
                    reason,
                    subject_id,
                ),
            )
            return self._subject_dict(cursor.fetchone())

    def reject(
        self,
        subject_id: UUID,
        *,
        expected_lifecycle_revision: int,
        expected_run_id: UUID | None = None,
        actor_type: str,
        actor_identifier: str | None,
        policy_version: str,
        reason_code: str,
        reason: str,
    ) -> dict[str, Any]:
        return self.promote(
            subject_id,
            "rejected",
            expected_lifecycle_revision=expected_lifecycle_revision,
            expected_run_id=expected_run_id,
            actor_type=actor_type,
            actor_identifier=actor_identifier,
            policy_version=policy_version,
            reason_code=reason_code,
            reason=reason,
        )

    def prepare_for_indexing(
        self,
        run_id: UUID,
        *,
        lifecycle_revision: int,
        actor_type: str = "orchestrator",
        actor_identifier: str | None = "IndexCheckpointService",
        policy_version: str = DEFAULT_POLICY_VERSION,
    ) -> AssetMembershipSeal:
        """Explicitly admit retained assets, then seal exact chunk membership.

        The compatibility policy admits every retained run asset because candidate
        ranking is outside issue #211. Each promotion commits separately, so an
        interrupted run resumes from the last durable stage.
        """
        self._assert_no_unknown_run_assets(run_id)
        while True:
            step = self._advance_one_indexing_admission(
                run_id,
                lifecycle_revision=lifecycle_revision,
                actor_type=actor_type,
                actor_identifier=actor_identifier,
                policy_version=policy_version,
            )
            if step is None:
                break
            self._after_promotion_step(step)
        return self.seal_completion_membership(
            run_id,
            lifecycle_revision=lifecycle_revision,
            actor_type=actor_type,
            actor_identifier=actor_identifier,
            policy_version=policy_version,
            reason_code="completion_membership_sealed",
            reason=(
                "Exact completion-critical PostgreSQL membership was sealed for "
                "indexing"
            ),
        )
