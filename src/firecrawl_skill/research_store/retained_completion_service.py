"""Exact retained-corpus completion membership for the research controller."""

from __future__ import annotations

from uuid import UUID

from .asset_promotion_models import (
    AssetMembershipSeal,
    AssetMembershipSealedError,
    AssetPromotionError,
    _canonical_sha256,
    _member_payload,
)
from .asset_promotion_service import AssetPromotionService
from .candidate_budget_outcomes import classify_persisted_completion_admission
from .candidate_policy_service import CandidatePolicyError, CandidatePolicyService


class _CoverageReviewCandidatePolicyService(CandidatePolicyService):
    """Reuse exact completion-budget semantics at the retained review boundary."""

    @staticmethod
    def _require_indexing_revision(uow, cursor, run_id, revision) -> None:
        state, current = uow.runs._lock_workflow_run(cursor, run_id)
        if state != "coverage_review":
            raise CandidatePolicyError(
                f"run {run_id} must be coverage_review for retained completion; "
                f"got {state}"
            )
        if int(current) != revision:
            raise CandidatePolicyError(
                "candidate budget revision is stale: "
                f"expected {revision}, current {current}"
            )


class RetainedCompletionPromotionService(AssetPromotionService):
    """Budget-admit and seal retained evidence before synthesis.

    The ordinary asset-promotion service deliberately authorizes its completion
    barrier only while a run is ``indexing``. A retained-corpus-sufficient run
    intentionally skips acquisition/extraction/indexing, so this specialized
    controller service reuses the exact same budget measurement, promotion, and
    seal semantics with one narrower lifecycle prerequisite: ``coverage_review``.
    It does not broaden or weaken the existing indexing path.
    """

    def __init__(self, uow_factory) -> None:
        super().__init__(uow_factory)
        self.candidate_policy_service = _CoverageReviewCandidatePolicyService(
            uow_factory
        )

    def prepare(
        self,
        run_id: UUID,
        *,
        lifecycle_revision: int,
        actor_type: str = "controller",
        actor_identifier: str | None = "ResearchWorkflowController",
        policy_version: str = "research-controller-v1",
    ) -> AssetMembershipSeal:
        """Promote and seal the exact retained set under coverage-review authority."""
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
                reason=(
                    "Retained corpus evidence admitted for exact completion-budget "
                    "evaluation"
                ),
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
            raise AssetPromotionError(
                "retained completion candidate budget was not accepted"
            )

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
                reason_code="retained_evidence_admitted_to_completion_barrier",
                reason=(
                    "Exact retained evidence set passed the persisted completion "
                    "candidate-budget check or explicit soft-limit override"
                ),
            )
            self._after_promotion_step(
                (UUID(str(promoted["id"])), str(promoted["current_stage"]))
            )

        return self._seal_retained_completion_membership(
            run_id,
            lifecycle_revision=lifecycle_revision,
            actor_type=actor_type,
            actor_identifier=actor_identifier,
            policy_version=policy_version,
        )

    def _seal_retained_completion_membership(
        self,
        run_id: UUID,
        *,
        lifecycle_revision: int,
        actor_type: str,
        actor_identifier: str | None,
        policy_version: str,
    ) -> AssetMembershipSeal:
        self._require_text(actor_type, "actor_type")
        self._require_text(policy_version, "policy_version")
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            state = self._lock_run(
                uow,
                cursor,
                run_id,
                lifecycle_revision,
                require_indexing=False,
            )
            if state != "coverage_review":
                raise AssetPromotionError(
                    f"run {run_id} must be coverage_review to seal retained "
                    f"completion membership; got {state}"
                )
            self._after_membership_lock(run_id)
            members = self._current_completion_members(uow, cursor, run_id)
            if not members:
                raise AssetPromotionError(
                    f"run {run_id} has no retained completion-critical assets to seal"
                )

            try:
                self.candidate_policy_service.require_matching_completion_check(
                    uow,
                    cursor,
                    run_id,
                    lifecycle_revision,
                    self.candidate_budget,
                    include_evidence=False,
                )
            except CandidatePolicyError as exc:
                raise AssetPromotionError(str(exc)) from exc

            membership_sha256 = _canonical_sha256(
                [
                    _member_payload(
                        member.subject_id,
                        member.snapshot_id,
                        member.role,
                        member.chunk_ids,
                    )
                    for member in members
                ]
            )
            expected_chunk_count = len(
                {chunk_id for member in members for chunk_id in member.chunk_ids}
            )
            active = self._load_active_seal(cursor, run_id, for_update=True)
            if active is not None:
                if active.lifecycle_revision != lifecycle_revision:
                    raise AssetMembershipSealedError(
                        "retained completion membership was sealed at another "
                        "lifecycle revision"
                    )
                if (
                    active.members == members
                    and active.membership_sha256 == membership_sha256
                    and active.expected_asset_count == len(members)
                    and active.expected_chunk_count == expected_chunk_count
                ):
                    return active
                raise AssetMembershipSealedError(
                    "retained completion-critical membership changed after sealing"
                )

            cursor.execute(
                """SELECT COALESCE(MAX(seal_revision),0)+1
                     FROM run_asset_membership_seals WHERE run_id=%s""",
                (run_id,),
            )
            seal_revision = int(cursor.fetchone()[0])
            cursor.execute(
                """INSERT INTO run_asset_membership_seals(
                       run_id,seal_revision,lifecycle_revision,membership_sha256,
                       expected_asset_count,expected_chunk_count,actor_type,
                       actor_identifier,policy_version,reason_code,reason)
                     VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                     RETURNING id""",
                (
                    run_id,
                    seal_revision,
                    lifecycle_revision,
                    membership_sha256,
                    len(members),
                    expected_chunk_count,
                    actor_type,
                    actor_identifier,
                    policy_version,
                    "retained_completion_membership_sealed",
                    (
                        "Exact retained completion-critical PostgreSQL membership "
                        "was budget-checked and sealed before synthesis"
                    ),
                ),
            )
            seal_id = UUID(str(cursor.fetchone()[0]))
            for ordinal, member in enumerate(members):
                cursor.execute(
                    """INSERT INTO run_asset_membership_members(
                           seal_id,run_id,subject_id,snapshot_id,role,ordinal,
                           chunk_ids,chunk_count,member_sha256)
                         VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        seal_id,
                        run_id,
                        member.subject_id,
                        member.snapshot_id,
                        member.role,
                        ordinal,
                        list(member.chunk_ids),
                        len(member.chunk_ids),
                        member.member_sha256,
                    ),
                )
            return AssetMembershipSeal(
                id=seal_id,
                run_id=run_id,
                seal_revision=seal_revision,
                lifecycle_revision=lifecycle_revision,
                status="sealed",
                membership_sha256=membership_sha256,
                expected_asset_count=len(members),
                expected_chunk_count=expected_chunk_count,
                members=members,
            )


__all__ = ["RetainedCompletionPromotionService"]
