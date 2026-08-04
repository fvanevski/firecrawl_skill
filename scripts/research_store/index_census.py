"""Exact PostgreSQL census for one sealed set of chunk index jobs.

The caller supplies the completion-critical chunk IDs and the active index
fingerprint.  PostgreSQL remains authoritative: Qdrant point counts and worker
heartbeat counters are not used to classify completion.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

CENSUS_CLASSES = (
    "complete",
    "claimable",
    "running_live",
    "running_expired",
    "retryable_failed",
    "dead",
    "missing_job",
    "wrong_fingerprint",
    "manifest_inconsistent",
)

NON_COMPLETE_CLASSES = tuple(
    state for state in CENSUS_CLASSES if state != "complete"
)

_CENSUS_SQL = """
WITH sealed AS (
    SELECT entity_id, ordinal::bigint
      FROM unnest(%s::uuid[]) WITH ORDINALITY AS member(entity_id, ordinal)
),
active_definition AS (
    SELECT id, physical_collection
      FROM index_definitions
     WHERE fingerprint=%s
),
entity_facts AS (
    SELECT sealed.entity_id,
           sealed.ordinal,
           (SELECT count(*) FROM active_definition)::integer
             AS active_definition_count,
           COALESCE(
             (
               SELECT jsonb_agg(
                        jsonb_build_object(
                          'manifest_id', manifest.id,
                          'chunk_id', manifest.chunk_id,
                          'index_definition_id', manifest.index_definition_id,
                          'index_status', manifest.index_status,
                          'qdrant_point_id', manifest.qdrant_point_id,
                          'qdrant_collection', manifest.qdrant_collection,
                          'indexed_at', manifest.indexed_at,
                          'error', manifest.error,
                          'physical_collection', definition.physical_collection,
                          'jobs', COALESCE(
                            (
                              SELECT jsonb_agg(
                                       jsonb_build_object(
                                         'job_id', job.id,
                                         'manifest_id', job.manifest_id,
                                         'index_definition_id',
                                           job.index_definition_id,
                                         'entity_type', job.entity_type,
                                         'entity_id', job.entity_id,
                                         'operation', job.operation,
                                         'index_name', job.index_name,
                                         'status', job.status,
                                         'attempt_count', job.attempt_count,
                                         'available_at', job.available_at,
                                         'lease_token', job.lease_token,
                                         'lease_owner', job.lease_owner,
                                         'lease_expires_at',
                                           job.lease_expires_at,
                                         'completed_at', job.completed_at,
                                         'error', job.error,
                                         'heartbeat_at', heartbeat.heartbeat_at
                                       )
                                       ORDER BY job.id
                                     )
                                FROM index_jobs job
                                LEFT JOIN index_worker_heartbeats heartbeat
                                  ON heartbeat.worker_id=job.lease_owner
                               WHERE job.manifest_id=manifest.id
                            ),
                            '[]'::jsonb
                          )
                        )
                        ORDER BY manifest.id
                      )
                 FROM embedding_manifests manifest
                 JOIN active_definition definition
                   ON definition.id=manifest.index_definition_id
                WHERE manifest.chunk_id=sealed.entity_id
             ),
             '[]'::jsonb
           ) AS active_manifests,
           COALESCE(
             (
               SELECT jsonb_agg(other.fingerprint ORDER BY other.fingerprint)
                 FROM (
                   SELECT DISTINCT definition.fingerprint
                     FROM embedding_manifests manifest
                     JOIN index_definitions definition
                       ON definition.id=manifest.index_definition_id
                    WHERE manifest.chunk_id=sealed.entity_id
                      AND definition.fingerprint<>%s
                 ) other
             ),
             '[]'::jsonb
           ) AS other_fingerprints
      FROM sealed
)
SELECT statement_timestamp() AS snapshot_at,
       entity_id,
       ordinal,
       active_definition_count,
       active_manifests,
       other_fingerprints
  FROM entity_facts
UNION ALL
SELECT statement_timestamp(),
       NULL::uuid,
       0::bigint,
       (SELECT count(*) FROM active_definition)::integer,
       '[]'::jsonb,
       '[]'::jsonb
 WHERE NOT EXISTS (SELECT 1 FROM sealed)
 ORDER BY ordinal
"""


def census_index_jobs(
    connection: Any,
    entity_ids: Sequence[UUID],
    fingerprint: str,
    *,
    max_attempts: int = 5,
    representative_limit: int = 20,
) -> dict[str, Any]:
    """Classify every sealed chunk exactly once from one SQL snapshot.

    ``entity_ids`` is the caller's sealed completion-critical membership.  The
    query reads all active-fingerprint manifests, their jobs, relevant worker
    heartbeat timestamps, and any competing fingerprints in one PostgreSQL
    statement.  Classification is then deterministic and count-conserving.
    """

    if not fingerprint or not fingerprint.strip():
        raise ValueError("fingerprint must be a non-empty string")
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    if representative_limit <= 0:
        raise ValueError("representative_limit must be positive")

    sealed = [_as_uuid(entity_id) for entity_id in entity_ids]
    if len(set(sealed)) != len(sealed):
        raise ValueError("sealed entity_ids must not contain duplicates")

    with connection.cursor() as cursor:
        cursor.execute(_CENSUS_SQL, (sealed, fingerprint, fingerprint))
        rows = cursor.fetchall()

    counts = {state: 0 for state in CENSUS_CLASSES}
    representatives = {state: [] for state in NON_COMPLETE_CLASSES}
    latest_heartbeat: tuple[datetime, str] | None = None
    lease_expirations: list[datetime] = []
    retry_availability: list[datetime] = []
    attempts_by_state: dict[str, list[int]] = {
        state: [] for state in CENSUS_CLASSES
    }
    snapshot_at: datetime | None = None

    observed = 0
    for row in rows:
        row_snapshot, entity_id, _ordinal, active_count, manifests, others = row
        current_snapshot = _as_datetime(row_snapshot)
        if snapshot_at is None:
            snapshot_at = current_snapshot
        elif current_snapshot != snapshot_at:
            raise RuntimeError("census rows were not read from one statement snapshot")

        if entity_id is None:
            continue
        observed += 1
        entity_text = str(_as_uuid(entity_id))
        state, job = _classify_entity(
            entity_text,
            int(active_count),
            _as_list(manifests),
            _as_list(others),
            snapshot_at,
            max_attempts,
        )
        counts[state] += 1
        if state != "complete" and len(representatives[state]) < representative_limit:
            representatives[state].append(entity_text)

        if job is None:
            continue
        attempt_count = _as_nonnegative_int(job.get("attempt_count"))
        if attempt_count is not None:
            attempts_by_state[state].append(attempt_count)

        if state in {"running_live", "running_expired"}:
            lease_expires_at = _optional_datetime(job.get("lease_expires_at"))
            if lease_expires_at is not None:
                lease_expirations.append(lease_expires_at)
            heartbeat_at = _optional_datetime(job.get("heartbeat_at"))
            lease_owner = job.get("lease_owner")
            if heartbeat_at is not None and isinstance(lease_owner, str):
                candidate = (heartbeat_at, lease_owner)
                if latest_heartbeat is None or candidate[0] > latest_heartbeat[0]:
                    latest_heartbeat = candidate

        if state in {"claimable", "retryable_failed"}:
            available_at = _optional_datetime(job.get("available_at"))
            if available_at is not None:
                retry_availability.append(available_at)

    expected = len(sealed)
    if observed != expected:
        raise RuntimeError(
            f"census returned {observed} members for sealed membership of {expected}"
        )
    conserved = sum(counts.values()) == expected
    if not conserved:
        raise RuntimeError("index-job census did not conserve sealed membership")

    result: dict[str, Any] = {
        "schema_version": "index-job-census-v1",
        "fingerprint": fingerprint,
        "sealed_entity_ids_sha256": _membership_digest(sealed),
        "snapshot_at": _iso(snapshot_at),
        "expected": expected,
        "complete_manifests": counts["complete"],
        "count_conserved": True,
        "all_complete": counts["complete"] == expected,
        "representative_limit": representative_limit,
        "representative_entity_ids": representatives,
        "latest_relevant_worker_heartbeat": (
            {
                "worker_id": latest_heartbeat[1],
                "heartbeat_at": _iso(latest_heartbeat[0]),
                "authoritative_for_counts": False,
            }
            if latest_heartbeat is not None
            else None
        ),
        "lease_expiration_bounds": _bounds(lease_expirations),
        "retry_available_at_bounds": _bounds(retry_availability),
        "attempt_count_bounds": {
            state: _numeric_bounds(values)
            for state, values in attempts_by_state.items()
            if values
        },
    }
    result.update(counts)
    result["counts"] = dict(counts)
    result["decision_evidence"] = _decision_evidence(counts, representatives)
    return result


def _classify_entity(
    entity_id: str,
    active_definition_count: int,
    manifests: list[Any],
    other_fingerprints: list[Any],
    snapshot_at: datetime,
    max_attempts: int,
) -> tuple[str, Mapping[str, Any] | None]:
    if active_definition_count == 0:
        return "wrong_fingerprint", None
    if active_definition_count != 1:
        return "manifest_inconsistent", None
    if not manifests:
        return (
            "wrong_fingerprint" if other_fingerprints else "missing_job",
            None,
        )
    if len(manifests) != 1:
        return "manifest_inconsistent", None

    manifest = manifests[0]
    if not isinstance(manifest, Mapping):
        return "manifest_inconsistent", None
    jobs = _as_list(manifest.get("jobs"))
    if not jobs:
        return "missing_job", None
    if len(jobs) != 1 or not isinstance(jobs[0], Mapping):
        return "manifest_inconsistent", None
    job = jobs[0]

    if not _topology_is_consistent(entity_id, manifest, job):
        return "manifest_inconsistent", job

    status = job.get("status")
    attempt_count = _as_nonnegative_int(job.get("attempt_count"))
    if attempt_count is None or status not in {
        "pending",
        "running",
        "failed",
        "dead",
        "complete",
    }:
        return "manifest_inconsistent", job
    if not _status_is_consistent(status, manifest, job):
        return "manifest_inconsistent", job

    if status == "complete":
        return "complete", job
    if status == "running":
        lease_expires_at = _optional_datetime(job.get("lease_expires_at"))
        if lease_expires_at is None:
            return "manifest_inconsistent", job
        if lease_expires_at > snapshot_at:
            return "running_live", job
        return ("dead" if attempt_count >= max_attempts else "running_expired", job)
    if status == "dead":
        return "dead", job
    if attempt_count >= max_attempts:
        return "dead", job
    if status == "failed":
        available_at = _optional_datetime(job.get("available_at"))
        if available_at is None:
            return "manifest_inconsistent", job
        return (
            "claimable" if available_at <= snapshot_at else "retryable_failed",
            job,
        )
    if status == "pending":
        available_at = _optional_datetime(job.get("available_at"))
        if available_at is not None and available_at > snapshot_at:
            return "manifest_inconsistent", job
        return "claimable", job
    return "manifest_inconsistent", job


def _topology_is_consistent(
    entity_id: str,
    manifest: Mapping[str, Any],
    job: Mapping[str, Any],
) -> bool:
    manifest_id = _text(manifest.get("manifest_id"))
    definition_id = _text(manifest.get("index_definition_id"))
    physical_collection = _text(manifest.get("physical_collection"))
    return bool(
        manifest_id
        and definition_id
        and physical_collection
        and _text(manifest.get("chunk_id")) == entity_id
        and _text(manifest.get("qdrant_point_id")) == entity_id
        and _text(manifest.get("qdrant_collection")) == physical_collection
        and _text(job.get("manifest_id")) == manifest_id
        and _text(job.get("index_definition_id")) == definition_id
        and _text(job.get("index_name")) == physical_collection
        and job.get("entity_type") == "chunk"
        and _text(job.get("entity_id")) == entity_id
        and job.get("operation") == "upsert"
    )


def _status_is_consistent(
    status: str,
    manifest: Mapping[str, Any],
    job: Mapping[str, Any],
) -> bool:
    manifest_status = manifest.get("index_status")
    lease_values = (
        job.get("lease_token"),
        job.get("lease_owner"),
        job.get("lease_expires_at"),
    )
    completed_at = job.get("completed_at")

    if status == "running":
        if not all(value not in (None, "") for value in lease_values):
            return False
        return (
            manifest_status in {"pending", "indexing", "failed"}
            and completed_at in (None, "")
        )

    if any(value not in (None, "") for value in lease_values):
        return False
    if status == "complete":
        return bool(
            manifest_status == "complete"
            and completed_at not in (None, "")
            and manifest.get("indexed_at") not in (None, "")
            and job.get("error") in (None, "")
            and manifest.get("error") in (None, "")
        )
    if completed_at not in (None, ""):
        return False
    if status == "pending":
        return manifest_status == "pending"
    if status in {"failed", "dead"}:
        return manifest_status == "failed"
    return False


def _decision_evidence(
    counts: Mapping[str, int], representatives: Mapping[str, list[str]]
) -> dict[str, Any]:
    mapping = {
        "wait": ("running_live",),
        "reclaim": ("running_expired",),
        "retry": ("claimable", "retryable_failed"),
        "fail": (
            "dead",
            "missing_job",
            "wrong_fingerprint",
            "manifest_inconsistent",
        ),
    }
    evidence: dict[str, Any] = {}
    for action, states in mapping.items():
        evidence[action] = {
            "count": sum(counts[state] for state in states),
            "class_counts": {state: counts[state] for state in states},
            "representative_entity_ids": {
                state: list(representatives.get(state, ())) for state in states
            },
        }
    return evidence


def _membership_digest(entity_ids: Iterable[UUID]) -> str:
    normalized = "\n".join(sorted(str(entity_id) for entity_id in entity_ids))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _bounds(values: Sequence[datetime]) -> dict[str, str] | None:
    if not values:
        return None
    return {"earliest": _iso(min(values)), "latest": _iso(max(values))}


def _numeric_bounds(values: Sequence[int]) -> dict[str, int]:
    return {"minimum": min(values), "maximum": max(values)}


def _as_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    raise RuntimeError(f"expected PostgreSQL JSON array, got {type(value).__name__}")


def _as_datetime(value: Any) -> datetime:
    parsed = _optional_datetime(value)
    if parsed is None:
        raise RuntimeError("census snapshot timestamp is missing")
    return parsed


def _optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise RuntimeError(f"invalid timestamp value {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()
