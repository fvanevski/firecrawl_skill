"""Connection-bound durable PostgreSQL index-job persistence."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from ...index_census import census_index_jobs


class PostgresIndexJobRepository:
    """Canonical durable index-job leasing and completion persistence."""

    def __init__(self, connection: Any) -> None:
        self.__connection = connection

    def claim_jobs(
        self,
        limit,
        lease_seconds=300,
        worker_id="compat",
        max_attempts=5,
        fingerprint=None,
        entity_ids=None,
    ):
        if limit <= 0 or lease_seconds <= 0 or max_attempts <= 0:
            raise ValueError("job limits and lease duration must be positive")
        with self.__connection.cursor() as cur:
            cur.execute(
                """UPDATE index_jobs SET status='dead',
                error='lease expired after final allowed attempt',
                lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                WHERE attempt_count >= %s AND (
                  (status='running' AND lease_expires_at < now())
                  OR status IN ('pending','failed'))
                RETURNING manifest_id,error""",
                (max_attempts,),
            )
            exhausted = cur.fetchall()
            if exhausted:
                cur.executemany(
                    """UPDATE embedding_manifests SET index_status='failed',error=%s
                    WHERE id=%s""",
                    [(error, manifest_id) for manifest_id, error in exhausted],
                )
            cur.execute(
                """WITH claimed AS (
                SELECT id FROM index_jobs
                WHERE ((status IN ('pending','failed') AND available_at <= now())
                    OR (status='running' AND lease_expires_at < now()))
                  AND attempt_count < %s
                  AND (%s::text IS NULL OR EXISTS(
                    SELECT 1 FROM index_definitions d
                    WHERE d.id=index_jobs.index_definition_id AND d.fingerprint=%s))
                  AND (%s::uuid[] IS NULL OR entity_id=ANY(%s::uuid[]))
                ORDER BY coalesce(lease_expires_at,available_at),created_at
                FOR UPDATE SKIP LOCKED LIMIT %s)
                UPDATE index_jobs j SET status='running',
                  started_at=coalesce(started_at,now()),attempt_count=attempt_count+1,
                  error=NULL,lease_token=gen_random_uuid(),lease_owner=%s,
                  lease_expires_at=now() + make_interval(secs => %s),updated_at=now()
                FROM claimed, embedding_manifests em, index_definitions d
                WHERE j.id=claimed.id AND em.id=j.manifest_id
                  AND d.id=j.index_definition_id
                RETURNING j.id,j.manifest_id,j.index_definition_id,j.entity_id,
                  j.operation,j.attempt_count,j.lease_token,d.fingerprint,
                  d.physical_collection,d.model_name,d.model_revision,d.dimension,
                  d.distance_metric,d.normalization,d.instruction_template_hash""",
                (
                    max_attempts,
                    fingerprint,
                    fingerprint,
                    entity_ids,
                    entity_ids,
                    limit,
                    worker_id,
                    lease_seconds,
                ),
            )
            keys = (
                "id",
                "manifest_id",
                "index_definition_id",
                "entity_id",
                "operation",
                "attempt_count",
                "lease_token",
                "fingerprint",
                "physical_collection",
                "model_name",
                "model_revision",
                "dimension",
                "distance_metric",
                "normalization",
                "instruction_template_hash",
            )
            return [
                {**dict(zip(keys, row)), "chunk_id": row[3]} for row in cur.fetchall()
            ]

    def renew_job(self, job_id, lease_token, lease_seconds=300):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self.__connection.cursor() as cur:
            cur.execute(
                """UPDATE index_jobs SET
                lease_expires_at=now() + make_interval(secs => %s),updated_at=now()
                WHERE id=%s AND status='running' AND lease_token=%s
                  AND lease_expires_at >= now()""",
                (lease_seconds, job_id, lease_token),
            )
            return cur.rowcount == 1

    def count_complete_manifests(self, chunk_ids, fingerprint):
        if not chunk_ids:
            return 0
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT count(DISTINCT em.chunk_id)
                FROM embedding_manifests em
                JOIN index_definitions d ON d.id=em.index_definition_id
                WHERE em.chunk_id=ANY(%s) AND d.fingerprint=%s
                  AND em.index_status='complete'""",
                (chunk_ids, fingerprint),
            )
            return int(cur.fetchone()[0])

    def finish_job(self, job_id, lease_token, error=None, max_attempts=5):
        if not isinstance(lease_token, UUID):
            raise TypeError(
                "finish_job requires the UUID lease token returned by claim_jobs"
            )
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT manifest_id,attempt_count FROM index_jobs
                WHERE id=%s AND status='running' AND lease_token=%s FOR UPDATE""",
                (job_id, lease_token),
            )
            row = cur.fetchone()
            if not row:
                return False
            manifest_id, attempt_count = row
            if error:
                status = "dead" if attempt_count >= max_attempts else "failed"
                cur.execute(
                    """UPDATE index_jobs SET status=%s,
                    available_at=now() + make_interval(
                      secs => least(3600,power(2,attempt_count)::int)),
                    error=%s,lease_token=NULL,lease_owner=NULL,
                    lease_expires_at=NULL,updated_at=now() WHERE id=%s""",
                    (status, error, job_id),
                )
                cur.execute(
                    "UPDATE embedding_manifests SET index_status='failed',error=%s WHERE id=%s",
                    (error, manifest_id),
                )
            else:
                cur.execute(
                    """UPDATE index_jobs SET status='complete',completed_at=now(),
                    error=NULL,lease_token=NULL,lease_owner=NULL,
                    lease_expires_at=NULL,updated_at=now() WHERE id=%s""",
                    (job_id,),
                )
                cur.execute(
                    """UPDATE embedding_manifests SET index_status='complete',
                    indexed_at=now(),error=NULL WHERE id=%s""",
                    (manifest_id,),
                )
            return True

    def heartbeat_worker(self, worker_id, metadata=None):
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO index_worker_heartbeats(worker_id,metadata)
                VALUES(%s,%s) ON CONFLICT(worker_id) DO UPDATE SET
                heartbeat_at=now(),metadata=excluded.metadata""",
                (worker_id, json.dumps(metadata or {})),
            )
            cur.execute(
                "DELETE FROM index_worker_heartbeats "
                "WHERE heartbeat_at < now()-interval '7 days'"
            )

    def worker_status(self):
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT worker_id,heartbeat_at,metadata
                FROM index_worker_heartbeats ORDER BY heartbeat_at DESC LIMIT 20"""
            )
            workers = [
                {"worker_id": row[0], "heartbeat_at": row[1], "metadata": row[2]}
                for row in cur.fetchall()
            ]
            cur.execute(
                """SELECT count(*) FILTER(WHERE status='running' AND lease_expires_at < now()),
                min(available_at) FILTER(WHERE status IN ('pending','failed')),
                count(*) FILTER(WHERE status='dead'),
                count(*) FILTER(WHERE status='running' AND lease_expires_at >= now())
                FROM index_jobs"""
            )
            stale, oldest, dead, active = cur.fetchone()
            return {
                "workers": workers,
                "stale_leases": stale,
                "oldest_pending": oldest,
                "dead_jobs": dead,
                "active_leases": active,
            }

    def census_index_jobs(
        self,
        entity_ids,
        fingerprint,
        *,
        max_attempts=5,
        representative_limit=20,
    ):
        """Run the authoritative sealed-set census on the UoW-owned connection."""
        return census_index_jobs(
            self.__connection,
            entity_ids,
            fingerprint,
            max_attempts=max_attempts,
            representative_limit=representative_limit,
        )
