"""Data contracts for durable indexing checkpoints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

IRRECOVERABLE_CLASSES = (
    "dead",
    "missing_job",
    "wrong_fingerprint",
    "manifest_inconsistent",
)
RECOVERABLE_CLASSES = (
    "claimable",
    "running_live",
    "running_expired",
    "retryable_failed",
)


class IndexCheckpointError(RuntimeError):
    """A persisted indexing checkpoint cannot be used safely."""


class IndexCheckpointStaleError(IndexCheckpointError):
    """The run revision, membership, or index definition changed."""


@dataclass(frozen=True)
class IndexCheckpoint:
    id: UUID
    run_id: UUID
    lifecycle_revision: int
    fingerprint: str
    entity_ids: tuple[UUID, ...]
    expected_membership_sha256: str
    expected_count: int
    complete_count: int
    manifest_count: int
    census_counts: dict[str, int]
    deadline_at: datetime | None
    status: str
    created_at: datetime
    updated_at: datetime
    last_observed_at: datetime | None
    completed_at: datetime | None
    invalidation_reason: str | None

    @property
    def recoverable_count(self) -> int:
        return sum(int(self.census_counts.get(name, 0)) for name in RECOVERABLE_CLASSES)

    @property
    def irrecoverable_count(self) -> int:
        return sum(
            int(self.census_counts.get(name, 0)) for name in IRRECOVERABLE_CLASSES
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "indexing-checkpoint-v1",
            "id": str(self.id),
            "run_id": str(self.run_id),
            "lifecycle_revision": self.lifecycle_revision,
            "fingerprint": self.fingerprint,
            "entity_ids": [str(value) for value in self.entity_ids],
            "expected_membership_sha256": self.expected_membership_sha256,
            "expected_count": self.expected_count,
            "complete_count": self.complete_count,
            "manifest_count": self.manifest_count,
            "census_counts": dict(self.census_counts),
            "deadline_at": _iso(self.deadline_at),
            "status": self.status,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "last_observed_at": _iso(self.last_observed_at),
            "completed_at": _iso(self.completed_at),
            "invalidation_reason": self.invalidation_reason,
        }


@dataclass(frozen=True)
class IndexFinalization:
    status: str
    checkpoint: IndexCheckpoint
    census: dict[str, Any]
    lifecycle_revision: int
    transition: dict[str, Any] | None = None
    reason: str | None = None

    @property
    def advanced(self) -> bool:
        return self.status in {"advanced", "reused"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "indexing-finalization-v1",
            "status": self.status,
            "reason": self.reason,
            "lifecycle_revision": self.lifecycle_revision,
            "checkpoint": self.checkpoint.to_dict(),
            "census": self.census,
            "transition": self.transition,
        }


def _checkpoint_from_row(row: tuple[Any, ...]) -> IndexCheckpoint:
    counts = row[9] or {}
    if isinstance(counts, str):
        counts = json.loads(counts)
    return IndexCheckpoint(
        id=UUID(str(row[0])),
        run_id=UUID(str(row[1])),
        lifecycle_revision=int(row[2]),
        fingerprint=str(row[3]),
        entity_ids=tuple(UUID(str(value)) for value in (row[4] or ())),
        expected_membership_sha256=str(row[5]),
        expected_count=int(row[6]),
        complete_count=int(row[7]),
        manifest_count=int(row[8]),
        census_counts={str(key): int(value) for key, value in counts.items()},
        deadline_at=_parse_datetime(row[10]),
        status=str(row[11]),
        created_at=_required_datetime(row[12]),
        updated_at=_required_datetime(row[13]),
        last_observed_at=_parse_datetime(row[14]),
        completed_at=_parse_datetime(row[15]),
        invalidation_reason=row[16],
    )


def _membership_digest(entity_ids: tuple[UUID, ...]) -> str:
    normalized = "\n".join(sorted(str(entity_id) for entity_id in entity_ids))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _required_datetime(value: Any) -> datetime:
    parsed = _parse_datetime(value)
    if parsed is None:
        raise IndexCheckpointError("checkpoint timestamp is missing")
    return parsed


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()
