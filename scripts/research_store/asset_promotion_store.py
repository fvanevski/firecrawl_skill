"""Internal mixin for PostgreSQL-authoritative asset promotion."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .asset_promotion_models import (
    AssetMembershipMember,
    AssetMembershipSeal,
    AssetPromotionCompatibilityError,
    AssetPromotionError,
    AssetPromotionPending,
    _canonical_sha256,
    _member_payload,
)


class _AssetPromotionStoreMixin:
    def _advance_one_indexing_admission(
        self,
        run_id: UUID,
        *,
        lifecycle_revision: int,
        actor_type: str,
        actor_identifier: str | None,
        policy_version: str,
    ) -> tuple[UUID, str] | None:
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            self._lock_run(
                uow,
                cursor,
                run_id,
                lifecycle_revision,
                require_indexing=True,
            )
            cursor.execute(
                """SELECT subject.id,subject.current_stage
                     FROM run_asset_promotion_subjects subject
                     JOIN research_run_assets asset
                       ON asset.run_id=subject.run_id
                      AND asset.snapshot_id=subject.snapshot_id
                      AND asset.role=subject.role
                    WHERE subject.run_id=%s
                      AND subject.current_stage IN ('retained','evidence_eligible')
                    ORDER BY subject.snapshot_id,subject.role,subject.id
                    LIMIT 1 FOR UPDATE OF subject""",
                (run_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            subject_id, current_stage = UUID(str(row[0])), str(row[1])
            if current_stage == "retained":
                target_stage = "evidence_eligible"
                reason_code = "retained_asset_admitted_as_evidence"
                reason = (
                    "Default non-ranking compatibility policy admitted a retained "
                    "run asset as evidence eligible"
                )
            else:
                target_stage = "completion_critical"
                reason_code = "evidence_asset_admitted_to_completion_barrier"
                reason = (
                    "Default non-ranking compatibility policy admitted the asset "
                    "to exact indexing completion membership"
                )
            cursor.execute(
                """UPDATE run_asset_promotion_subjects
                      SET current_stage=%s,actor_type=%s,actor_identifier=%s,
                          policy_version=%s,lifecycle_revision=%s,
                          reason_code=%s,reason=%s
                    WHERE id=%s""",
                (
                    target_stage,
                    actor_type,
                    actor_identifier,
                    policy_version,
                    lifecycle_revision,
                    reason_code,
                    reason,
                    subject_id,
                ),
            )
            return subject_id, target_stage

    def _assert_no_unknown_run_assets(self, run_id: UUID) -> None:
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT count(*)
                     FROM research_run_assets asset
                     LEFT JOIN run_asset_promotion_subjects subject
                       ON subject.run_id=asset.run_id
                      AND subject.snapshot_id=asset.snapshot_id
                      AND subject.role=asset.role
                    WHERE asset.run_id=%s AND subject.id IS NULL""",
                (run_id,),
            )
            unknown_count = int(cursor.fetchone()[0])
        if unknown_count:
            raise AssetPromotionCompatibilityError(
                f"run {run_id} has {unknown_count} historical run asset(s) with "
                "unknown promotion stage; apply an evidence-bearing forward repair"
            )

    def _current_completion_members(
        self, uow, cursor, run_id: UUID
    ) -> tuple[AssetMembershipMember, ...]:
        cursor.execute(
            """SELECT subject.id,subject.snapshot_id,subject.role
                 FROM run_asset_promotion_subjects subject
                 JOIN research_run_assets asset
                   ON asset.run_id=subject.run_id
                  AND asset.snapshot_id=subject.snapshot_id
                  AND asset.role=subject.role
                WHERE subject.run_id=%s
                  AND subject.current_stage='completion_critical'
                ORDER BY subject.snapshot_id,subject.role,subject.id""",
            (run_id,),
        )
        members: list[AssetMembershipMember] = []
        for subject_id, snapshot_id, role in cursor.fetchall():
            chunk_ids = self._chunk_ids(uow, cursor, UUID(str(snapshot_id)))
            if not chunk_ids:
                raise AssetPromotionPending(
                    f"completion-critical asset {snapshot_id}/{role} has no "
                    "matching chunks for the configured derivation"
                )
            payload = _member_payload(
                UUID(str(subject_id)),
                UUID(str(snapshot_id)),
                str(role),
                chunk_ids,
            )
            members.append(
                AssetMembershipMember(
                    subject_id=UUID(str(subject_id)),
                    snapshot_id=UUID(str(snapshot_id)),
                    role=str(role),
                    chunk_ids=chunk_ids,
                    member_sha256=_canonical_sha256(payload),
                )
            )
        return tuple(members)

    @staticmethod
    def _chunk_ids(uow, cursor, snapshot_id: UUID) -> tuple[UUID, ...]:
        cursor.execute(
            """SELECT DISTINCT chunk.id
                 FROM documents document
                 JOIN chunks chunk ON chunk.document_id=document.id
                WHERE document.snapshot_id=%s
                  AND document.parser_version=%s
                  AND document.normalization_version=%s
                  AND chunk.chunker_version=%s
                ORDER BY chunk.id""",
            (
                snapshot_id,
                uow.parser_version,
                uow.normalization_version,
                uow.chunker_version,
            ),
        )
        return tuple(UUID(str(row[0])) for row in cursor.fetchall())

    @staticmethod
    def _load_active_seal(
        cursor, run_id: UUID, *, for_update: bool = False
    ) -> AssetMembershipSeal | None:
        cursor.execute(
            """SELECT id,seal_revision,lifecycle_revision,status,
                      membership_sha256,expected_asset_count,expected_chunk_count
                 FROM run_asset_membership_seals
                WHERE run_id=%s AND status='sealed'"""
            + (" FOR UPDATE" if for_update else ""),
            (run_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        (
            seal_id,
            seal_revision,
            lifecycle_revision,
            status,
            membership_sha256,
            expected_asset_count,
            expected_chunk_count,
        ) = row
        cursor.execute(
            """SELECT subject_id,snapshot_id,role,chunk_ids,member_sha256
                 FROM run_asset_membership_members
                WHERE seal_id=%s ORDER BY ordinal""",
            (seal_id,),
        )
        members = tuple(
            AssetMembershipMember(
                subject_id=UUID(str(subject_id)),
                snapshot_id=UUID(str(snapshot_id)),
                role=str(role),
                chunk_ids=tuple(UUID(str(item)) for item in chunk_ids),
                member_sha256=str(member_sha256),
            )
            for subject_id, snapshot_id, role, chunk_ids, member_sha256
            in cursor.fetchall()
        )
        return AssetMembershipSeal(
            id=UUID(str(seal_id)),
            run_id=run_id,
            seal_revision=int(seal_revision),
            lifecycle_revision=int(lifecycle_revision),
            status=str(status),
            membership_sha256=str(membership_sha256),
            expected_asset_count=int(expected_asset_count),
            expected_chunk_count=int(expected_chunk_count),
            members=members,
        )

    @staticmethod
    def _lock_run(
        uow,
        cursor,
        run_id: UUID,
        lifecycle_revision: int,
        *,
        require_indexing: bool,
    ) -> str:
        state, current_revision = uow.runs._lock_workflow_run(cursor, run_id)
        if int(current_revision) != lifecycle_revision:
            raise AssetPromotionError(
                "asset promotion lifecycle revision is stale: "
                f"expected {lifecycle_revision}, current {current_revision}"
            )
        if require_indexing and state != "indexing":
            raise AssetPromotionError(
                f"run {run_id} must be indexing to seal membership; got {state}"
            )
        return str(state)

    @staticmethod
    def _subject_dict(row) -> dict[str, Any]:
        names = (
            "id",
            "candidate_id",
            "snapshot_id",
            "role",
            "current_stage",
            "stage_revision",
            "provenance",
            "actor_type",
            "actor_identifier",
            "policy_version",
            "lifecycle_revision",
            "reason_code",
            "reason",
            "created_at",
            "updated_at",
        )
        result = dict(zip(names, row, strict=True))
        for name in ("id", "candidate_id", "snapshot_id"):
            if result[name] is not None:
                result[name] = str(result[name])
        return result

    def _subject_by_id(self, cursor, subject_id: UUID) -> dict[str, Any]:
        cursor.execute(
            """SELECT id,candidate_id,snapshot_id,role,current_stage,
                      stage_revision,provenance,actor_type,actor_identifier,
                      policy_version,lifecycle_revision,reason_code,reason,
                      created_at,updated_at
                 FROM run_asset_promotion_subjects WHERE id=%s""",
            (subject_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(subject_id)
        return self._subject_dict(row)

    @staticmethod
    def _require_text(value: str, field: str) -> None:
        if not value or not value.strip():
            raise ValueError(f"{field} must be non-empty")

    def _after_membership_lock(self, run_id: UUID) -> None:
        """Deterministic concurrency-test seam while the run row lock is held."""

    def _after_promotion_step(self, step: tuple[UUID, str]) -> None:
        """Failure-injection seam after one durable promotion transition."""
