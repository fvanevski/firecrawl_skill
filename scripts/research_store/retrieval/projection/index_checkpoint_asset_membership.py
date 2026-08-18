"""Internal mixin for durable PostgreSQL indexing checkpoints."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ...asset_promotion_service import AssetMembershipSeal
from .index_checkpoint_models import IndexCheckpoint, IndexCheckpointStaleError


class _IndexCheckpointAssetMembershipMixin:
    def _prepare_asset_membership_if_needed(
        self, run_id: UUID, lifecycle_revision: int
    ) -> AssetMembershipSeal | None:
        """Prepare only checkpoints created after the 0040 seal contract.

        An already-active pre-0040 checkpoint follows the explicit compatibility
        read path and is never assigned fabricated historical promotion state.
        """
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            cursor.execute(
                "SELECT state,lifecycle_revision FROM research_runs WHERE id=%s",
                (run_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise KeyError(run_id)
            state, current_revision = str(row[0]), int(row[1])
            active = self._latest(cursor, run_id, statuses=("active",))
        if state != "indexing" or active is not None:
            return None
        if current_revision != lifecycle_revision:
            raise IndexCheckpointStaleError(
                "checkpoint revision is stale: "
                f"expected {lifecycle_revision}, current {current_revision}"
            )
        return self.asset_promotions.prepare_for_indexing(
            run_id,
            lifecycle_revision=lifecycle_revision,
        )

    @staticmethod
    def _validate_asset_binding(
        cursor, checkpoint_id: UUID, asset_seal: AssetMembershipSeal
    ) -> None:
        cursor.execute(
            """SELECT lifecycle_revision,asset_membership_seal_id,
                      asset_membership_sha256,asset_expected_count,
                      asset_expected_chunk_count
                 FROM indexing_checkpoints WHERE id=%s""",
            (checkpoint_id,),
        )
        row = cursor.fetchone()
        expected = (
            asset_seal.lifecycle_revision,
            asset_seal.id,
            asset_seal.membership_sha256,
            asset_seal.expected_asset_count,
            asset_seal.expected_chunk_count,
        )
        if row != expected:
            raise IndexCheckpointStaleError(
                "checkpoint asset-membership binding changed"
            )

    @staticmethod
    def _completion_payload(
        checkpoint: IndexCheckpoint,
        census: dict[str, Any],
        *,
        manifest_count: int,
        asset_seal: AssetMembershipSeal | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "indexing_checkpoint_id": str(checkpoint.id),
            "membership_sha256": checkpoint.expected_membership_sha256,
            "fingerprint": checkpoint.fingerprint,
            "expected": checkpoint.expected_count,
            "complete": checkpoint.complete_count,
            "manifest_count": manifest_count,
            "census": census,
        }
        if asset_seal is not None:
            payload.update(
                {
                    "asset_membership_sha256": asset_seal.membership_sha256,
                    "asset_membership_seal_id": str(asset_seal.id),
                    "asset_seal_revision": asset_seal.seal_revision,
                    "asset_expected": asset_seal.expected_asset_count,
                    "asset_expected_chunk_count": (asset_seal.expected_chunk_count),
                    "asset_membership_schema_version": (
                        "completion-critical-membership-v1"
                    ),
                }
            )
        return payload
