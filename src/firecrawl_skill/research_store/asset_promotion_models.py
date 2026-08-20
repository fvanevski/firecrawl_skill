"""Models and errors for authoritative asset promotion."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID


class AssetPromotionError(RuntimeError):
    """An asset promotion or completion-membership invariant failed."""


class AssetPromotionPending(AssetPromotionError):
    """A completion-critical asset lacks its required PostgreSQL derivation."""


class AssetPromotionCompatibilityError(AssetPromotionError):
    """Historical rows lack evidence needed for an authoritative promotion."""


class AssetMembershipSealedError(AssetPromotionError):
    """Completion membership must be reopened before it can change."""


@dataclass(frozen=True)
class AssetMembershipMember:
    subject_id: UUID
    snapshot_id: UUID
    role: str
    chunk_ids: tuple[UUID, ...]
    member_sha256: str


@dataclass(frozen=True)
class AssetMembershipSeal:
    id: UUID
    run_id: UUID
    seal_revision: int
    lifecycle_revision: int
    status: str
    membership_sha256: str
    expected_asset_count: int
    expected_chunk_count: int
    members: tuple[AssetMembershipMember, ...]

    @property
    def chunk_ids(self) -> tuple[UUID, ...]:
        return tuple(
            sorted(
                {chunk_id for member in self.members for chunk_id in member.chunk_ids},
                key=str,
            )
        )


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _member_payload(
    subject_id: UUID,
    snapshot_id: UUID,
    role: str,
    chunk_ids: tuple[UUID, ...],
) -> dict[str, Any]:
    return {
        "subject_id": str(subject_id),
        "snapshot_id": str(snapshot_id),
        "role": role,
        "chunk_ids": [str(chunk_id) for chunk_id in chunk_ids],
    }
