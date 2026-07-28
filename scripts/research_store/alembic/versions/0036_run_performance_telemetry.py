# ruff: noqa: UP031
"""Add run-scoped performance telemetry tables (issue #143).

This migration introduces schema required for authoritative, run-scoped
performance telemetry in the release benchmark.

## Schema changes

### run_performance_telemetry

Aggregated performance telemetry summary for a single research run.
Each run has exactly one row.

* ``run_id`` — UUID PK, FK to research_runs
* ``schema_version`` — telemetry schema version (always ``"performance-telemetry-summary-v1"``)
* ``total_tokens`` — sum of all token counts for the run
* ``token_source`` — source of token counts: ``endpoint``, ``tokenizer``, or ``unavailable``
* ``semantic_calls`` — total semantic calls for the run
* ``cache_lookups`` — total cache lookups for the run
* ``cache_hits`` — total cache hits for the run
* ``cache_misses`` — total cache misses for the run
* ``cache_hit_rate`` — cache hit rate (0.0–1.0), nullable
* ``embedding_batch_count`` — total embedding batches
* ``embedding_vector_count`` — total embedding vectors produced
* ``embedding_elapsed_seconds`` — total embedding wall-clock time
* ``embedding_throughput`` — texts per second (computed from batch count and elapsed)
* ``cpu_samples`` — number of CPU samples collected
* ``cpu_mean_percent`` — mean CPU usage, nullable
* ``cpu_max_percent`` — maximum CPU usage, nullable
* ``gpu_samples`` — number of GPU samples collected
* ``gpu_mean_memory_mb`` — mean GPU memory, nullable
* ``gpu_max_memory_mb`` — maximum GPU memory, nullable
* ``gpu_unavailable`` — whether GPU telemetry is unavailable
* ``strict_pass`` — whether all required metrics are measured
* ``created_at`` — Unix timestamp
* ``updated_at`` — Unix timestamp

Constraints:
* PK on ``run_id``.
* FK to ``research_runs(id)`` ON DELETE CASCADE.
* ``ck_telemetry_token_source`` — token_source must be one of: endpoint, tokenizer, unavailable.
* ``ck_telemetry_strict_pass`` — strict_pass must be boolean.

### run_cache_events

Append-only log of cache events scoped to a run and semantic stage.

* ``id`` — UUID PK
* ``run_id`` — UUID, FK to research_runs
* ``stage`` — semantic stage (outline, binding, draft, citation_pass)
* ``event_type`` — lookup, hit, miss, invalidation, reuse
* ``key_hash`` — SHA-256 key hash
* ``model_fingerprint`` — model fingerprint
* ``hit`` — whether the lookup was a hit (nullable for invalidation/reuse)
* ``created_at`` — Unix timestamp

Constraints:
* ``ck_cache_event_type`` — event_type must be one of: lookup, hit, miss, invalidation, reuse.
* Index on (run_id, stage) for efficient run-scoped queries.
* Index on (run_id, event_type) for aggregation.

### run_embedding_throughput

Per-stage embedding throughput records.

* ``id`` — UUID PK
* ``run_id`` — UUID, FK to research_runs
* ``stage`` — stage name (e.g. "embedding", "indexing")
* ``batch_count`` — number of batch requests
* ``vector_count`` — vectors produced (excluding failures)
* ``failed_count`` — failed embedding requests
* ``total_texts`` — total input texts processed
* ``elapsed_seconds`` — wall-clock time in embedding calls
* ``endpoint_url`` — embedding endpoint URL
* ``endpoint_model`` — model name
* ``dimension`` — vector dimension, nullable
* ``created_at`` — Unix timestamp

Constraints:
* Index on (run_id, stage) for efficient lookups.

### run_resource_samples

Multi-sample CPU/GPU telemetry collected across the run window.

* ``id`` — UUID PK
* ``run_id`` — UUID, FK to research_runs
* ``device_type`` — cpu or gpu
* ``device_index`` — hardware device index (0-based)
* ``device_uuid`` — hardware UUID, nullable
* ``sample_type`` — measurement type (e.g. "cpu_percent", "gpu_memory_used_mb")
* ``value`` — measured value
* ``sample_at`` — ISO-8601 timestamp
* ``collector`` — library used (psutil, pynvml)
* ``collector_version`` — library version
* ``sample_number`` — sequential sample number
* ``status`` — measured, unavailable, partial, stale, invalid
* ``created_at`` — Unix timestamp

Constraints:
* ``ck_resource_device_type`` — device_type must be cpu or gpu.
* ``ck_resource_status`` — status must be one of: measured, unavailable, partial, stale, invalid.
* Index on (run_id, device_type, sample_type) for aggregation.

## Design decisions

* The telemetry summary is a single-row-per-run table — one INSERT or
  UPSERT per run. Aggregation is done by the telemetry service, not by
  the benchmark runner.
* Cache events are append-only — each lookup produces one row.
* Embedding throughput is per-stage, allowing comparison across stages.
* Resource samples are append-only — the telemetry service collects
  multiple samples over the run window.
* All tables use ``created_at`` as a Unix timestamp (double precision)
  for consistency with the semantic_cache table.
* Forward-only migration. No data is written to existing runs.

## Compatibility

* Forward-only migration. No existing tables are modified.
* Phase 1–6 data is preserved.
"""

from alembic import op

revision = "0036_run_performance_telemetry"
down_revision = "0035_index_point_counts"
branch_labels = None
depends_on = None

TELEMETRY_TOKEN_SOURCES = ("endpoint", "tokenizer", "unavailable")
CACHE_EVENT_TYPES = ("lookup", "hit", "miss", "invalidation", "reuse")
CACHE_STAGES = ("outline", "binding", "draft", "citation_pass", "indexing")
RESOURCE_DEVICE_TYPES = ("cpu", "gpu")
RESOURCE_STATUSES = ("measured", "unavailable", "partial", "stale", "invalid")


def upgrade():
    # ------------------------------------------------------------------
    # 1. run_performance_telemetry — aggregated summary per run
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS run_performance_telemetry (
          run_id                    uuid NOT NULL,
          schema_version            text NOT NULL DEFAULT 'performance-telemetry-summary-v1',
          total_tokens              bigint NOT NULL DEFAULT 0,
          token_source              text NOT NULL DEFAULT 'unavailable'
                                      CHECK (token_source IN %s),
          semantic_calls            bigint NOT NULL DEFAULT 0,
          cache_lookups             bigint NOT NULL DEFAULT 0,
          cache_hits                bigint NOT NULL DEFAULT 0,
          cache_misses              bigint NOT NULL DEFAULT 0,
          cache_hit_rate            double precision,
          embedding_batch_count     bigint NOT NULL DEFAULT 0,
          embedding_vector_count    bigint NOT NULL DEFAULT 0,
          embedding_elapsed_seconds double precision NOT NULL DEFAULT 0.0,
          embedding_throughput      double precision NOT NULL DEFAULT 0.0,
          cpu_samples               bigint NOT NULL DEFAULT 0,
          cpu_mean_percent          double precision,
          cpu_max_percent           double precision,
          gpu_samples               bigint NOT NULL DEFAULT 0,
          gpu_mean_memory_mb        double precision,
          gpu_max_memory_mb         double precision,
          gpu_unavailable           boolean NOT NULL DEFAULT true,
          strict_pass               boolean NOT NULL DEFAULT true,
          created_at                double precision NOT NULL,
          updated_at                double precision NOT NULL,

          PRIMARY KEY (run_id),
          CONSTRAINT fk_telemetry_run
            FOREIGN KEY (run_id) REFERENCES research_runs(id)
            ON DELETE CASCADE
        );
        """
        % (TELEMETRY_TOKEN_SOURCES,)
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'run_performance_telemetry' AND indexname = 'idx_telemetry_updated'
          ) THEN
            CREATE INDEX idx_telemetry_updated
              ON run_performance_telemetry (updated_at);
          END IF;
        END $$;
        """
    )

    # ------------------------------------------------------------------
    # 2. run_cache_events — append-only cache event log
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS run_cache_events (
          id                      uuid NOT NULL DEFAULT gen_random_uuid(),
          run_id                  uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
          stage                   text NOT NULL CHECK (stage IN %s),
          event_type              text NOT NULL CHECK (event_type IN %s),
          key_hash                text,
          model_fingerprint       text,
          hit                     boolean,
          created_at              double precision NOT NULL,

          PRIMARY KEY (id)
        );
        """
        % (CACHE_STAGES, CACHE_EVENT_TYPES)
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'run_cache_events' AND indexname = 'idx_cache_events_run_stage'
          ) THEN
            CREATE INDEX idx_cache_events_run_stage
              ON run_cache_events (run_id, stage);
          END IF;
        END $$;
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'run_cache_events' AND indexname = 'idx_cache_events_run_type'
          ) THEN
            CREATE INDEX idx_cache_events_run_type
              ON run_cache_events (run_id, event_type);
          END IF;
        END $$;
        """
    )

    # ------------------------------------------------------------------
    # 3. run_embedding_throughput — per-stage embedding records
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS run_embedding_throughput (
          id                      uuid NOT NULL DEFAULT gen_random_uuid(),
          run_id                  uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
          stage                   text NOT NULL,
          batch_count             bigint NOT NULL DEFAULT 0,
          vector_count            bigint NOT NULL DEFAULT 0,
          failed_count            bigint NOT NULL DEFAULT 0,
          total_texts             bigint NOT NULL DEFAULT 0,
          elapsed_seconds         double precision NOT NULL DEFAULT 0.0,
          endpoint_url            text NOT NULL DEFAULT '',
          endpoint_model          text NOT NULL DEFAULT '',
          dimension               bigint,
          created_at              double precision NOT NULL,

          PRIMARY KEY (id)
        );
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'run_embedding_throughput' AND indexname = 'idx_embedding_run_stage'
          ) THEN
            CREATE INDEX idx_embedding_run_stage
              ON run_embedding_throughput (run_id, stage);
          END IF;
        END $$;
        """
    )

    # ------------------------------------------------------------------
    # 4. run_resource_samples — CPU/GPU multi-sample telemetry
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS run_resource_samples (
          id                  uuid NOT NULL DEFAULT gen_random_uuid(),
          run_id              uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
          device_type         text NOT NULL CHECK (device_type IN %s),
          device_index        integer NOT NULL DEFAULT 0,
          device_uuid         text,
          sample_type         text NOT NULL,
          value               double precision NOT NULL DEFAULT 0.0,
          sample_at           text NOT NULL,
          collector           text NOT NULL DEFAULT '',
          collector_version   text NOT NULL DEFAULT '',
          sample_number       bigint NOT NULL DEFAULT 0,
          status              text NOT NULL DEFAULT 'measured'
                              CHECK (status IN %s),
          created_at          double precision NOT NULL,

          PRIMARY KEY (id)
        );
        """
        % (RESOURCE_DEVICE_TYPES, RESOURCE_STATUSES)
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'run_resource_samples' AND indexname = 'idx_resource_run_type'
          ) THEN
            CREATE INDEX idx_resource_run_type
              ON run_resource_samples (run_id, device_type, sample_type);
          END IF;
        END $$;
        """
    )

    # ------------------------------------------------------------------
    # 5. endpoint_usage_records — per-call token usage
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS endpoint_usage_records (
          id                  uuid NOT NULL DEFAULT gen_random_uuid(),
          run_id              uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
          call_id             uuid NOT NULL,
          endpoint_type       text NOT NULL CHECK (endpoint_type IN %s),
          provider            text NOT NULL DEFAULT '',
          model               text NOT NULL DEFAULT '',
          model_revision      text NOT NULL DEFAULT '',
          prompt_tokens       bigint NOT NULL DEFAULT 0,
          completion_tokens   bigint NOT NULL DEFAULT 0,
          total_tokens        bigint NOT NULL DEFAULT 0,
          source              text NOT NULL DEFAULT 'unavailable'
                              CHECK (source IN %s),
          created_at          double precision NOT NULL,

          PRIMARY KEY (id)
        );
        """
        % (
            ("generative", "embedding", "reranking"),
            ("endpoint", "tokenizer", "unavailable"),
        )
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'endpoint_usage_records' AND indexname = 'idx_endpoint_usage_run'
          ) THEN
            CREATE INDEX idx_endpoint_usage_run
              ON endpoint_usage_records (run_id, call_id);
          END IF;
        END $$;
        """
    )

    op.execute(
        "INSERT INTO schema_migrations(version) VALUES (36) ON CONFLICT DO NOTHING"
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v36 recovery boundary or apply a forward repair "
        "migration."
    )
