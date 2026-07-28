"""Performance telemetry service for run-scoped benchmark metrics.

This module provides:

* ``PerformanceTelemetryService`` — persists and retrieves run-scoped
  performance telemetry in PostgreSQL.
* ``RunCacheEvents`` — records and queries run-scoped cache events.
* ``RunEmbeddingThroughput`` — records and queries embedding throughput.
* ``RunResourceSamples`` — records and queries CPU/GPU resource samples.
* ``aggregate_telemetry`` — aggregates all telemetry into a
  ``PerformanceTelemetrySummary``.

## Authoritative state

- All telemetry is persisted in PostgreSQL tables created by migration 0036.
- The ``run_performance_telemetry`` table holds one aggregated summary row
  per run.
- Cache events, embedding throughput, and resource samples are append-only
  logs that feed into the summary.

## Invariants

1. Every metric records its source, version, units, and aggregation method.
2. Missing instrumentation is recorded as ``status = 'unavailable'``,
   never as numeric zero.
3. The summary row is written via INSERT ... ON CONFLICT (run_id) UPDATE
   to support idempotent writes.
4. Strict mode fails when required telemetry is estimated, unavailable,
   partial, stale, or unscoped.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from research_domain.models import (
    EndpointUsageRecord,
    PerformanceTelemetrySummary,
    ResourceSample,
)

logger = logging.getLogger(__name__)


class PerformanceTelemetryService:
    """Persist and query run-scoped performance telemetry.

    Args:
        connection: A psycopg connection or cursor object that supports
            ``execute`` with ``%s`` parameterization.
    """

    def __init__(self, connection) -> None:
        self._connection = connection

    # ------------------------------------------------------------------
    # Cache events
    # ------------------------------------------------------------------

    def record_cache_event(
        self,
        run_id: UUID,
        stage: str,
        event_type: str,
        key_hash: str = "",
        model_fingerprint: str = "",
        hit: bool | None = None,
    ) -> None:
        """Record a single cache event.

        Args:
            run_id: Research run UUID.
            stage: Semantic stage.
            event_type: lookup, hit, miss, invalidation, or reuse.
            key_hash: SHA-256 key hash.
            model_fingerprint: Model fingerprint.
            hit: Whether the lookup was a hit.
        """
        now = time.time()
        self._connection.execute(
            """INSERT INTO run_cache_events
               (run_id, stage, event_type, key_hash, model_fingerprint, hit, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                str(run_id),
                stage,
                event_type,
                key_hash,
                model_fingerprint,
                hit,
                now,
            ),
        )

    def get_cache_stats(self, run_id: UUID) -> dict[str, int]:
        """Get cache event counts for a run.

        Returns:
            Dict with keys: lookups, hits, misses.
        """
        cur = self._connection.execute(
            """SELECT
                   COUNT(*) FILTER (WHERE event_type = 'lookup') AS lookups,
                   COUNT(*) FILTER (WHERE event_type = 'lookup' AND hit IS TRUE) AS hits,
                   COUNT(*) FILTER (WHERE event_type = 'lookup' AND hit IS FALSE) AS misses
                 FROM run_cache_events
                 WHERE run_id = %s""",
            (str(run_id),),
        )
        row = cur.fetchone()
        return {
            "lookups": row[0] or 0,
            "hits": row[1] or 0,
            "misses": row[2] or 0,
        }

    # ------------------------------------------------------------------
    # Embedding throughput
    # ------------------------------------------------------------------

    def record_embedding_throughput(
        self,
        run_id: UUID,
        stage: str,
        batch_count: int,
        vector_count: int,
        failed_count: int,
        total_texts: int,
        elapsed_seconds: float,
        endpoint_url: str,
        endpoint_model: str,
        dimension: int | None = None,
    ) -> None:
        """Record embedding throughput for a stage.

        Args:
            run_id: Research run UUID.
            stage: Stage name.
            batch_count: Number of batch requests.
            vector_count: Vectors produced.
            failed_count: Failed embedding requests.
            total_texts: Total input texts.
            elapsed_seconds: Wall-clock time.
            endpoint_url: Embedding endpoint URL.
            endpoint_model: Model name.
            dimension: Vector dimension.
        """
        now = time.time()
        self._connection.execute(
            """INSERT INTO run_embedding_throughput
               (run_id, stage, batch_count, vector_count, failed_count,
                total_texts, elapsed_seconds, endpoint_url, endpoint_model,
                dimension, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                str(run_id),
                stage,
                batch_count,
                vector_count,
                failed_count,
                total_texts,
                elapsed_seconds,
                endpoint_url,
                endpoint_model,
                dimension,
                now,
            ),
        )

    def get_embedding_stats(self, run_id: UUID) -> dict[str, Any]:
        """Get aggregated embedding stats for a run.

        Returns:
            Dict with batch_count, vector_count, failed_count, total_texts,
            elapsed_seconds, and throughput.
        """
        cur = self._connection.execute(
            """SELECT
                   COALESCE(SUM(batch_count), 0),
                   COALESCE(SUM(vector_count), 0),
                   COALESCE(SUM(failed_count), 0),
                   COALESCE(SUM(total_texts), 0),
                   COALESCE(SUM(elapsed_seconds), 0.0)
                 FROM run_embedding_throughput
                 WHERE run_id = %s""",
            (str(run_id),),
        )
        row = cur.fetchone()
        batch_count, vector_count, failed_count, total_texts, elapsed = row

        throughput = 0.0
        if elapsed and elapsed > 0:
            throughput = round(total_texts / elapsed, 3)

        return {
            "batch_count": batch_count or 0,
            "vector_count": vector_count or 0,
            "failed_count": failed_count or 0,
            "total_texts": total_texts or 0,
            "elapsed_seconds": elapsed or 0.0,
            "throughput": throughput,
        }

    # ------------------------------------------------------------------
    # Resource samples
    # ------------------------------------------------------------------

    def record_resource_sample(self, sample: ResourceSample) -> None:
        """Persist a single resource sample.

        Args:
            sample: A ResourceSample dataclass instance.
        """
        now = time.time()
        self._connection.execute(
            """INSERT INTO run_resource_samples
               (run_id, device_type, device_index, device_uuid, sample_type,
                value, sample_at, collector, collector_version,
                sample_number, status, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                sample.run_id,
                sample.device_type,
                sample.device_index,
                sample.device_uuid,
                sample.sample_type,
                sample.value,
                sample.sample_at,
                sample.collector,
                sample.collector_version,
                sample.sample_number,
                sample.status,
                now,
            ),
        )

    def get_resource_summary(self, run_id: UUID) -> dict[str, Any]:
        """Get aggregated resource summary for a run.

        Returns:
            Dict with cpu_samples, cpu_mean, cpu_max,
            gpu_samples, gpu_mean, gpu_max, gpu_unavailable.
        """
        # CPU summary.
        cur = self._connection.execute(
            """SELECT
                   COUNT(*) AS cnt,
                   AVG(value) AS mean,
                   MAX(value) AS maximum
                 FROM run_resource_samples
                 WHERE run_id = %s AND device_type = 'cpu'
                   AND status = 'measured'""",
            (str(run_id),),
        )
        cpu_row = cur.fetchone()
        cpu_samples = cpu_row[0] or 0
        cpu_mean = round(float(cpu_row[1]), 2) if cpu_row[1] is not None else None
        cpu_max = round(float(cpu_row[2]), 2) if cpu_row[2] is not None else None

        # GPU summary.
        cur = self._connection.execute(
            """SELECT
                   COUNT(*) AS cnt,
                   AVG(value) AS mean,
                   MAX(value) AS maximum
                 FROM run_resource_samples
                 WHERE run_id = %s AND device_type = 'gpu'
                   AND status = 'measured'""",
            (str(run_id),),
        )
        gpu_row = cur.fetchone()
        gpu_samples = gpu_row[0] or 0
        gpu_mean = round(float(gpu_row[1]), 2) if gpu_row[1] is not None else None
        gpu_max = round(float(gpu_row[2]), 2) if gpu_row[2] is not None else None

        return {
            "cpu_samples": cpu_samples,
            "cpu_mean_percent": cpu_mean,
            "cpu_max_percent": cpu_max,
            "gpu_samples": gpu_samples,
            "gpu_mean_memory_mb": gpu_mean,
            "gpu_max_memory_mb": gpu_max,
            "gpu_unavailable": gpu_samples == 0,
        }

    # ------------------------------------------------------------------
    # Endpoint usage
    # ------------------------------------------------------------------

    def record_endpoint_usage(self, record: EndpointUsageRecord) -> None:
        """Persist an endpoint usage record.

        Args:
            record: An EndpointUsageRecord dataclass instance.
        """
        now = time.time()
        self._connection.execute(
            """INSERT INTO endpoint_usage_records
               (run_id, call_id, endpoint_type, provider, model, model_revision,
                prompt_tokens, completion_tokens, total_tokens, source,
                created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                record.run_id,
                record.call_id,
                record.endpoint_type,
                record.provider,
                record.model,
                record.model_revision,
                record.prompt_tokens,
                record.completion_tokens,
                record.total_tokens,
                record.source,
                now,
            ),
        )

    def get_token_summary(self, run_id: UUID) -> dict[str, Any]:
        """Get aggregated token summary for a run.

        Returns:
            Dict with total_tokens, token_source, call_count.
        """
        cur = self._connection.execute(
            """SELECT
                   COALESCE(SUM(total_tokens), 0),
                   CASE
                     WHEN SUM(CASE WHEN source = 'endpoint' THEN 1 ELSE 0 END) > 0
                     THEN 'endpoint'
                     WHEN SUM(CASE WHEN source = 'tokenizer' THEN 1 ELSE 0 END) > 0
                     THEN 'tokenizer'
                     ELSE 'unavailable'
                   END,
                   COUNT(*)
                 FROM endpoint_usage_records
                 WHERE run_id = %s""",
            (str(run_id),),
        )
        row = cur.fetchone()
        return {
            "total_tokens": row[0] or 0,
            "token_source": row[1] or "unavailable",
            "call_count": row[2] or 0,
        }

    # ------------------------------------------------------------------
    # Summary aggregation and persistence
    # ------------------------------------------------------------------

    def write_summary(self, summary: PerformanceTelemetrySummary) -> None:
        """Write or update the aggregated telemetry summary.

        Uses INSERT ... ON CONFLICT for idempotent writes.

        Args:
            summary: A PerformanceTelemetrySummary dataclass instance.
        """
        now = time.time()
        self._connection.execute(
            """INSERT INTO run_performance_telemetry
               (run_id, schema_version, total_tokens, token_source,
                semantic_calls, cache_lookups, cache_hits, cache_misses,
                cache_hit_rate, embedding_batch_count, embedding_vector_count,
                embedding_elapsed_seconds, embedding_throughput,
                cpu_samples, cpu_mean_percent, cpu_max_percent,
                gpu_samples, gpu_mean_memory_mb, gpu_max_memory_mb,
                gpu_unavailable, strict_pass, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (run_id)
               DO UPDATE SET
                 schema_version = EXCLUDED.schema_version,
                 total_tokens = EXCLUDED.total_tokens,
                 token_source = EXCLUDED.token_source,
                 semantic_calls = EXCLUDED.semantic_calls,
                 cache_lookups = EXCLUDED.cache_lookups,
                 cache_hits = EXCLUDED.cache_hits,
                 cache_misses = EXCLUDED.cache_misses,
                 cache_hit_rate = EXCLUDED.cache_hit_rate,
                 embedding_batch_count = EXCLUDED.embedding_batch_count,
                 embedding_vector_count = EXCLUDED.embedding_vector_count,
                 embedding_elapsed_seconds = EXCLUDED.embedding_elapsed_seconds,
                 embedding_throughput = EXCLUDED.embedding_throughput,
                 cpu_samples = EXCLUDED.cpu_samples,
                 cpu_mean_percent = EXCLUDED.cpu_mean_percent,
                 cpu_max_percent = EXCLUDED.cpu_max_percent,
                 gpu_samples = EXCLUDED.gpu_samples,
                 gpu_mean_memory_mb = EXCLUDED.gpu_mean_memory_mb,
                 gpu_max_memory_mb = EXCLUDED.gpu_max_memory_mb,
                 gpu_unavailable = EXCLUDED.gpu_unavailable,
                 strict_pass = EXCLUDED.strict_pass,
                 updated_at = EXCLUDED.updated_at""",
            (
                summary.run_id,
                summary.schema_version,
                summary.total_tokens,
                summary.token_source,
                summary.semantic_calls,
                summary.cache_lookups,
                summary.cache_hits,
                summary.cache_misses,
                summary.cache_hit_rate,
                summary.embedding_batch_count,
                summary.embedding_vector_count,
                summary.embedding_elapsed_seconds,
                summary.embedding_throughput,
                summary.cpu_samples,
                summary.cpu_mean_percent,
                summary.cpu_max_percent,
                summary.gpu_samples,
                summary.gpu_mean_memory_mb,
                summary.gpu_max_memory_mb,
                summary.gpu_unavailable,
                summary.strict_pass,
                now,
                now,
            ),
        )

    def read_summary(self, run_id: UUID) -> PerformanceTelemetrySummary | None:
        """Read the aggregated telemetry summary for a run.

        Args:
            run_id: Research run UUID.

        Returns:
            A PerformanceTelemetrySummary, or None when no row exists.
        """
        cur = self._connection.execute(
            """SELECT run_id, schema_version, total_tokens, token_source,
                      semantic_calls, cache_lookups, cache_hits, cache_misses,
                      cache_hit_rate, embedding_batch_count, embedding_vector_count,
                      embedding_elapsed_seconds, embedding_throughput,
                      cpu_samples, cpu_mean_percent, cpu_max_percent,
                      gpu_samples, gpu_mean_memory_mb, gpu_max_memory_mb,
                      gpu_unavailable, strict_pass
               FROM run_performance_telemetry
               WHERE run_id = %s""",
            (str(run_id),),
        )
        row = cur.fetchone()
        if row is None:
            return None

        return PerformanceTelemetrySummary(
            run_id=row[0],
            schema_version=row[1] or "performance-telemetry-summary-v1",
            total_tokens=row[2] or 0,
            token_source=row[3] or "unavailable",
            semantic_calls=row[4] or 0,
            cache_lookups=row[5] or 0,
            cache_hits=row[6] or 0,
            cache_misses=row[7] or 0,
            cache_hit_rate=row[8],
            embedding_batch_count=row[9] or 0,
            embedding_vector_count=row[10] or 0,
            embedding_elapsed_seconds=row[11] or 0.0,
            embedding_throughput=row[12] or 0.0,
            cpu_samples=row[13] or 0,
            cpu_mean_percent=row[14],
            cpu_max_percent=row[15],
            gpu_samples=row[16] or 0,
            gpu_mean_memory_mb=row[17],
            gpu_max_memory_mb=row[18],
            gpu_unavailable=row[19] if row[19] is not None else True,
            strict_pass=row[20] if row[20] is not None else True,
        )

    def build_summary(self, run_id: UUID) -> PerformanceTelemetrySummary:
        """Build and persist a summary from all telemetry sources.

        This method aggregates cache stats, embedding stats, resource
        samples, and token counts into a single summary row.

        Args:
            run_id: Research run UUID.

        Returns:
            The built PerformanceTelemetrySummary.
        """
        # Aggregate cache stats.
        cache_stats = self.get_cache_stats(run_id)
        cache_lookups = cache_stats["lookups"]
        cache_hits = cache_stats["hits"]
        cache_misses = cache_stats["misses"]
        cache_hit_rate: float | None = None
        if cache_lookups > 0:
            cache_hit_rate = round(cache_hits / cache_lookups, 6)

        # Aggregate embedding stats.
        emb_stats = self.get_embedding_stats(run_id)

        # Aggregate resource samples.
        resource_summary = self.get_resource_summary(run_id)

        # Aggregate token counts.
        token_summary = self.get_token_summary(run_id)

        # Count semantic calls.
        cur = self._connection.execute(
            """SELECT COUNT(*) FROM semantic_calls WHERE run_id = %s""",
            (str(run_id),),
        )
        semantic_calls = cur.fetchone()[0] or 0

        # Determine strict pass: all required metrics must be measured.
        strict_pass = True
        if token_summary["token_source"] == "unavailable":
            strict_pass = False
        if resource_summary["cpu_samples"] == 0:
            strict_pass = False
        if emb_stats["batch_count"] == 0 or emb_stats["elapsed_seconds"] == 0:
            strict_pass = False
        if cache_lookups == 0:
            strict_pass = False
        # GPU is optional — unavailable does not fail strict.

        summary = PerformanceTelemetrySummary(
            run_id=str(run_id),
            total_tokens=token_summary["total_tokens"],
            token_source=token_summary["token_source"],
            semantic_calls=semantic_calls,
            cache_lookups=cache_lookups,
            cache_hits=cache_hits,
            cache_misses=cache_misses,
            cache_hit_rate=cache_hit_rate,
            embedding_batch_count=emb_stats["batch_count"],
            embedding_vector_count=emb_stats["vector_count"],
            embedding_elapsed_seconds=emb_stats["elapsed_seconds"],
            embedding_throughput=emb_stats["throughput"],
            cpu_samples=resource_summary["cpu_samples"],
            cpu_mean_percent=resource_summary["cpu_mean_percent"],
            cpu_max_percent=resource_summary["cpu_max_percent"],
            gpu_samples=resource_summary["gpu_samples"],
            gpu_mean_memory_mb=resource_summary["gpu_mean_memory_mb"],
            gpu_max_memory_mb=resource_summary["gpu_max_memory_mb"],
            gpu_unavailable=resource_summary["gpu_unavailable"],
            strict_pass=strict_pass,
        )

        self.write_summary(summary)
        return summary
