"""Internal mixin for PostgreSQL-authoritative asset promotion."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .asset_promotion_models import (
    AssetMembershipSeal,
    AssetMembershipSealedError,
    AssetPromotionError,
)
from .candidate_budget_outcomes import classify_persisted_completion_admission
from .candidate_policy_service import CandidatePolicyError, decision_error_message

DEFAULT_POLICY_VERSION = "completion-membership-v1"


class _AssetPromotionCoreMixin:
    def curation_census(
        self,
        uow: Any,
        run_id: UUID,
        *,
        lifecycle_revision: int,
        for_update: bool,
    ) -> list[dict[str, Any]]:
        """Return the exact mutable evidence census for one curated decision."""
        with uow.connection.cursor() as cursor:
            state = self._lock_run(
                uow,
                cursor,
                run_id,
                lifecycle_revision,
                require_indexing=False,
            )
            if state not in {"retrieving", "acquiring", "indexing"}:
                raise AssetPromotionError(
                    f"run {run_id} is not at a curated-selection boundary: {state}"
                )
            cursor.execute(
                """SELECT id,snapshot_id,role,current_stage,stage_revision
                     FROM run_asset_promotion_subjects
                    WHERE run_id=%s AND current_stage<>'rejected'
                    ORDER BY snapshot_id,role,id"""
                + (" FOR UPDATE" if for_update else ""),
                (run_id,),
            )
            census = [
                {
                    "subject_id": str(subject_id),
                    "snapshot_id": str(snapshot_id),
                    "role": str(role),
                    "current_stage": str(current_stage),
                    "stage_revision": int(stage_revision),
                }
                for subject_id, snapshot_id, role, current_stage, stage_revision
                in cursor.fetchall()
            ]
        unsupported = {
            item["current_stage"]
            for item in census
            if item["current_stage"] not in {"extracted", "retained"}
        }
        if unsupported:
            raise AssetPromotionError(
                "curated selection requires complete extraction/retention disposition; "
                f"unexpected stages: {sorted(unsupported)}"
            )
        return census

    def apply_curated_selection(
        self,
        uow: Any,
        run_id: UUID,
        *,
        lifecycle_revision: int,
        retain_subject_ids: set[UUID],
        reason: str,
        actor_identifier: str,
    ) -> None:
        """Apply one complete curated keep/reject decision in the caller transaction."""
        census = self.curation_census(
            uow,
            run_id,
            lifecycle_revision=lifecycle_revision,
            for_update=True,
        )
        allowed = {UUID(item["subject_id"]) for item in census}
        if not retain_subject_ids <= allowed:
            raise AssetPromotionError(
                "curated selection references a subject outside the exact run census"
            )
        with uow.connection.cursor() as cursor:
            for item in census:
                subject_id = UUID(item["subject_id"])
                current_stage = str(item["current_stage"])
                if subject_id in retain_subject_ids:
                    if current_stage == "retained":
                        continue
                    target_stage = "retained"
                    reason_code = "operator_curated_retention"
                else:
                    target_stage = "rejected"
                    reason_code = "operator_curated_rejection"
                cursor.execute(
                    """UPDATE run_asset_promotion_subjects
                          SET current_stage=%s,actor_type='operator',
                              actor_identifier=%s,policy_version=%s,
                              lifecycle_revision=%s,reason_code=%s,reason=%s
                        WHERE id=%s""",
                    (
                        target_stage,
                        actor_identifier,
                        "operator-action-policy-v1",
                        lifecycle_revision,
                        reason_code,
                        reason,
                        subject_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise AssetPromotionError(
                        f"curated promotion subject disappeared: {subject_id}"
                    )

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
        """Apply one legal stage transition with lifecycle CAS and budget gates."""
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
                        "completion membership is sealed; reopen it before changing membership"
                    )
            if target_stage == "completion_critical":
                try:
                    self.candidate_policy_service.require_matching_completion_check(
                        uow,
                        cursor,
                        run_id,
                        expected_lifecycle_revision,
                        self.candidate_budget,
                        include_evidence=True,
                    )
                except CandidatePolicyError as exc:
                    raise AssetPromotionError(str(exc)) from exc
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
        completion_admission_preview_id: UUID | None = None,
    ) -> AssetMembershipSeal:
        """Admit retained assets only after an auditable full-set budget check.

        Retained assets first become ``evidence_eligible``. The exact proposed
        evidence set is then checked and persisted. Hard-limit violations fail
        closed. Soft-limit violations remain blocked until an override tied to
        that exact check is recorded. Only an accepted check permits promotion
        to ``completion_critical`` and sealing.
        """
        self._assert_no_unknown_run_assets(run_id)

        while True:
            retained = next(
                (
                    item
                    for item in self.list_assets(run_id)
                    if item.get("id") and item.get("current_stage") == "retained"
                ),
                None,
            )
            if retained is None:
                break
            promoted = self.promote(
                UUID(str(retained["id"])),
                "evidence_eligible",
                expected_lifecycle_revision=lifecycle_revision,
                expected_run_id=run_id,
                actor_type=actor_type,
                actor_identifier=actor_identifier,
                policy_version=policy_version,
                reason_code="retained_asset_admitted_as_evidence",
                reason="Retained asset admitted for candidate-budget evaluation",
            )
            self._after_promotion_step(
                (UUID(str(promoted["id"])), str(promoted["current_stage"]))
            )

        try:
            decision = self.candidate_policy_service.evaluate_completion_admission(
                run_id,
                lifecycle_revision,
                self.candidate_budget,
            )
            if completion_admission_preview_id is not None:
                decision = (
                    self.candidate_policy_service.rebind_completion_admission_override(
                        run_id,
                        decision.check_id,
                        completion_admission_preview_id,
                        lifecycle_revision,
                    )
                )
        except CandidatePolicyError as exc:
            raise AssetPromotionError(str(exc)) from exc
        if not decision.accepted:
            boundary = classify_persisted_completion_admission(
                self.candidate_policy_service,
                run_id,
                lifecycle_revision,
                check_id=decision.check_id,
            )
            if boundary is not None:
                raise boundary
            raise AssetPromotionError(decision_error_message(decision))

        while True:
            evidence = next(
                (
                    item
                    for item in self.list_assets(run_id)
                    if item.get("id")
                    and item.get("current_stage") == "evidence_eligible"
                ),
                None,
            )
            if evidence is None:
                break
            promoted = self.promote(
                UUID(str(evidence["id"])),
                "completion_critical",
                expected_lifecycle_revision=lifecycle_revision,
                expected_run_id=run_id,
                actor_type=actor_type,
                actor_identifier=actor_identifier,
                policy_version=policy_version,
                reason_code="candidate_budget_admitted_to_completion_barrier",
                reason=(
                    "Exact proposed completion set passed the persisted candidate "
                    "budget check or explicit soft-limit override"
                ),
            )
            self._after_promotion_step(
                (UUID(str(promoted["id"])), str(promoted["current_stage"]))
            )

        return self.seal_completion_membership(
            run_id,
            lifecycle_revision=lifecycle_revision,
            actor_type=actor_type,
            actor_identifier=actor_identifier,
            policy_version=policy_version,
            reason_code="completion_membership_sealed",
            reason=(
                "Exact completion-critical PostgreSQL membership was budget-checked "
                "and sealed for indexing"
            ),
        )
