"""Internal mixin for PostgreSQL-authoritative asset promotion."""

from __future__ import annotations

from uuid import UUID

from .asset_promotion_models import (
    AssetMembershipSeal,
    AssetMembershipSealedError,
    AssetPromotionError,
    _canonical_sha256,
    _member_payload,
)


class _AssetPromotionSealMixin:
    def seal_completion_membership(
        self,
        run_id: UUID,
        *,
        lifecycle_revision: int,
        actor_type: str,
        actor_identifier: str | None,
        policy_version: str,
        reason_code: str,
        reason: str | None,
    ) -> AssetMembershipSeal:
        self._require_text(actor_type, "actor_type")
        self._require_text(policy_version, "policy_version")
        self._require_text(reason_code, "reason_code")
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            self._lock_run(
                uow,
                cursor,
                run_id,
                lifecycle_revision,
                require_indexing=True,
            )
            self._after_membership_lock(run_id)
            members = self._current_completion_members(uow, cursor, run_id)
            if not members:
                raise AssetPromotionError(
                    f"run {run_id} has no completion-critical assets to seal"
                )
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
                {
                    chunk_id
                    for member in members
                    for chunk_id in member.chunk_ids
                }
            )
            active = self._load_active_seal(cursor, run_id, for_update=True)
            if active is not None:
                if active.lifecycle_revision != lifecycle_revision:
                    raise AssetMembershipSealedError(
                        "completion membership was sealed at another lifecycle "
                        "revision; explicitly reopen it before resealing"
                    )
                if (
                    active.members == members
                    and active.membership_sha256 == membership_sha256
                    and active.expected_asset_count == len(members)
                    and active.expected_chunk_count == expected_chunk_count
                ):
                    return active
                raise AssetMembershipSealedError(
                    "completion-critical membership changed after sealing; "
                    "explicitly reopen it before resealing"
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
                    reason_code,
                    reason,
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

    def reopen_completion_membership(
        self,
        run_id: UUID,
        *,
        expected_lifecycle_revision: int,
        actor_type: str,
        actor_identifier: str | None,
        policy_version: str,
        reason_code: str,
        reason: str,
    ) -> AssetMembershipSeal:
        """Explicitly reopen membership and invalidate its active checkpoint."""
        self._require_text(actor_type, "actor_type")
        self._require_text(policy_version, "policy_version")
        self._require_text(reason_code, "reason_code")
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            self._lock_run(
                uow,
                cursor,
                run_id,
                expected_lifecycle_revision,
                require_indexing=True,
            )
            seal = self._load_active_seal(cursor, run_id, for_update=True)
            if seal is None:
                raise AssetPromotionError(
                    f"run {run_id} has no sealed completion membership to reopen"
                )
            cursor.execute(
                """UPDATE run_asset_membership_seals
                      SET status='reopened',reopened_at=now(),
                          reopened_lifecycle_revision=%s,
                          reopened_actor_type=%s,reopened_actor_identifier=%s,
                          reopened_policy_version=%s,reopened_reason_code=%s,
                          reopened_reason=%s,
                          reopened_transaction_id=pg_current_xact_id()
                    WHERE id=%s AND status='sealed'""",
                (
                    expected_lifecycle_revision,
                    actor_type,
                    actor_identifier,
                    policy_version,
                    reason_code,
                    reason,
                    seal.id,
                ),
            )
            if cursor.rowcount != 1:
                raise AssetPromotionError("completion membership reopen lost its CAS")
            return AssetMembershipSeal(
                id=seal.id,
                run_id=seal.run_id,
                seal_revision=seal.seal_revision,
                lifecycle_revision=seal.lifecycle_revision,
                status="reopened",
                membership_sha256=seal.membership_sha256,
                expected_asset_count=seal.expected_asset_count,
                expected_chunk_count=seal.expected_chunk_count,
                members=seal.members,
            )

    def get_active_seal(self, run_id: UUID) -> AssetMembershipSeal | None:
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            return self._load_active_seal(cursor, run_id)

    def load_active_seal_in_transaction(
        self, cursor, run_id: UUID, *, for_update: bool = False
    ) -> AssetMembershipSeal | None:
        return self._load_active_seal(cursor, run_id, for_update=for_update)
