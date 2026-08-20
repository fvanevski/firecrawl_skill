"""PostgreSQL persistence helpers for indexing checkpoints."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from ...index_census import CENSUS_CLASSES
from .index_checkpoint_models import (
    IndexCheckpoint,
    IndexCheckpointError,
    IndexCheckpointStaleError,
    _checkpoint_from_row,
    _parse_datetime,
)


class IndexCheckpointStoreMixin:
    def _current_membership(self, uow, cursor, run_id: UUID) -> tuple[UUID, ...]:
        cursor.execute(
            """SELECT DISTINCT chunk.id
                 FROM research_run_assets asset
                 JOIN documents document ON document.snapshot_id=asset.snapshot_id
                 JOIN chunks chunk ON chunk.document_id=document.id
                WHERE asset.run_id=%s
                  AND document.parser_version=%s
                  AND document.normalization_version=%s
                  AND chunk.chunker_version=%s
                ORDER BY chunk.id""",
            (run_id, uow.parser_version, uow.normalization_version, uow.chunker_version),
        )
        return tuple(UUID(str(row[0])) for row in cursor.fetchall())

    @staticmethod
    def _definition_count(cursor, fingerprint: str) -> int:
        cursor.execute("SELECT count(*) FROM index_definitions WHERE fingerprint=%s", (fingerprint,))
        return int(cursor.fetchone()[0])

    @staticmethod
    def _manifest_count(cursor, entity_ids: tuple[UUID, ...], fingerprint: str) -> int:
        if not entity_ids:
            return 0
        cursor.execute(
            """SELECT count(DISTINCT manifest.id)
                 FROM embedding_manifests manifest
                 JOIN index_definitions definition ON definition.id=manifest.index_definition_id
                WHERE manifest.chunk_id=ANY(%s) AND definition.fingerprint=%s""",
            (list(entity_ids), fingerprint),
        )
        return int(cursor.fetchone()[0])

    def _validate_census(self, checkpoint: IndexCheckpoint, census: dict[str, Any]) -> None:
        if census.get("fingerprint") != checkpoint.fingerprint:
            raise IndexCheckpointStaleError("census fingerprint changed")
        if census.get("sealed_entity_ids_sha256") != checkpoint.expected_membership_sha256:
            raise IndexCheckpointStaleError("census membership hash changed")
        if int(census.get("expected", -1)) != checkpoint.expected_count:
            raise IndexCheckpointStaleError("census expected count changed")
        counts = census.get("counts")
        if not isinstance(counts, dict):
            counts = {name: census.get(name) for name in CENSUS_CLASSES}
        normalized: dict[str, int] = {}
        for name in CENSUS_CLASSES:
            value = counts.get(name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise IndexCheckpointError(f"invalid census count for {name}")
            normalized[name] = value
        if sum(normalized.values()) != checkpoint.expected_count:
            raise IndexCheckpointError("census does not conserve sealed membership")

    def _write_observation(
        self,
        cursor,
        checkpoint: IndexCheckpoint,
        census: dict[str, Any],
        *,
        manifest_count: int,
        deadline_at: datetime | None = None,
    ) -> IndexCheckpoint:
        self._validate_census(checkpoint, census)
        counts = {name: int((census.get("counts") or census)[name]) for name in CENSUS_CLASSES}
        observed_at = _parse_datetime(census.get("snapshot_at")) or datetime.now(timezone.utc)
        heartbeat = census.get("latest_relevant_worker_heartbeat")
        lease_bounds = census.get("lease_expiration_bounds")
        retry_bounds = census.get("retry_available_at_bounds")
        cursor.execute(
            """UPDATE indexing_checkpoints
                  SET complete_count=%s, manifest_count=%s, census_counts=%s,
                      latest_relevant_worker_heartbeat=%s, lease_expiration_bounds=%s,
                      retry_available_at_bounds=%s, deadline_at=COALESCE(%s,deadline_at),
                      last_observed_at=%s, updated_at=now()
                WHERE id=%s""",
            (
                counts["complete"], manifest_count, json.dumps(counts, sort_keys=True),
                json.dumps(heartbeat), json.dumps(lease_bounds), json.dumps(retry_bounds),
                deadline_at, observed_at, checkpoint.id,
            ),
        )
        cursor.execute(
            """INSERT INTO indexing_checkpoint_observations(
                   checkpoint_id,observed_at,expected_count,complete_count,
                   manifest_count,census_counts,latest_relevant_worker_heartbeat,
                   lease_expiration_bounds,retry_available_at_bounds)
                 VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                checkpoint.id, observed_at, checkpoint.expected_count, counts["complete"],
                manifest_count, json.dumps(counts, sort_keys=True), json.dumps(heartbeat),
                json.dumps(lease_bounds), json.dumps(retry_bounds),
            ),
        )
        return self._by_id(cursor, checkpoint.id, for_update=True)

    def _invalidate(self, cursor, checkpoint: IndexCheckpoint, reason: str) -> IndexCheckpoint:
        cursor.execute(
            """UPDATE indexing_checkpoints
                  SET status='invalidated', invalidation_reason=%s,
                      invalidated_at=now(), updated_at=now()
                WHERE id=%s AND status='active'""",
            (reason, checkpoint.id),
        )
        return self._by_id(cursor, checkpoint.id, for_update=True)

    @staticmethod
    def _checkpoint_census(checkpoint: IndexCheckpoint) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": "index-job-census-v1",
            "fingerprint": checkpoint.fingerprint,
            "sealed_entity_ids_sha256": checkpoint.expected_membership_sha256,
            "expected": checkpoint.expected_count,
            "complete": checkpoint.complete_count,
            "complete_manifests": checkpoint.complete_count,
            "counts": dict(checkpoint.census_counts),
        }
        result.update(checkpoint.census_counts)
        return result

    def _latest(self, cursor, run_id: UUID, *, statuses: tuple[str, ...], for_update: bool = False) -> IndexCheckpoint | None:
        cursor.execute(
            """SELECT id,run_id,lifecycle_revision,fingerprint,entity_ids,
                      expected_membership_sha256,expected_count,complete_count,
                      manifest_count,census_counts,deadline_at,status,created_at,
                      updated_at,last_observed_at,completed_at,invalidation_reason
                 FROM indexing_checkpoints
                WHERE run_id=%s AND status=ANY(%s)
                ORDER BY created_at DESC,id DESC LIMIT 1"""
            + (" FOR UPDATE" if for_update else ""),
            (run_id, list(statuses)),
        )
        row = cursor.fetchone()
        return None if row is None else _checkpoint_from_row(row)

    def _by_id(self, cursor, checkpoint_id: UUID, *, for_update: bool = False) -> IndexCheckpoint:
        cursor.execute(
            """SELECT id,run_id,lifecycle_revision,fingerprint,entity_ids,
                      expected_membership_sha256,expected_count,complete_count,
                      manifest_count,census_counts,deadline_at,status,created_at,
                      updated_at,last_observed_at,completed_at,invalidation_reason
                 FROM indexing_checkpoints WHERE id=%s"""
            + (" FOR UPDATE" if for_update else ""),
            (checkpoint_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(checkpoint_id)
        return _checkpoint_from_row(row)
