"""Add model_endpoints table for resource governance.

This migration introduces the schema required by issue #66 (P7-06).

## Schema changes

### model_endpoints

Tracks health state for each model endpoint (generative, embedding, reranker).
Each row is identified by ``endpoint_name`` (unique).

* ``endpoint_name`` — text PK, one of: generative, embedding, reranker
* ``url`` — text, the endpoint URL
* ``status`` — text, one of: healthy, degraded, unhealthy, unknown
* ``last_check_at`` — double precision (Unix timestamp), nullable
* ``last_error`` — text, nullable
* ``concurrent_requests`` — bigint, defaults to 0
* ``queued_requests`` — bigint, defaults to 0
* ``total_checks`` — bigint, defaults to 0
* ``total_failures`` — bigint, defaults to 0
* ``degraded_since`` — double precision (Unix timestamp), nullable
* ``restart_count`` — bigint, defaults to 0

Constraints:
* ``pk_model_endpoints`` — primary key on ``endpoint_name``.
* ``ck_model_endpoints_status`` — status must be one of:
  healthy, degraded, unhealthy, unknown.

## Design decisions

* The table is upsert-only: health state is updated by ``endpoint_name``.
* All columns are nullable except ``endpoint_name`` and ``status`` to
  support gradual initialization — a newly registered endpoint starts
  with defaults.
* ``concurrent_requests`` and ``queued_requests`` are application-managed
  counters; PostgreSQL acts as crash-recovery storage, not the primary
  concurrency authority.
* ``degraded_since`` tracks when an endpoint entered degraded state,
  enabling operational dashboards to detect prolonged degradation.

## Compatibility

* Forward-only migration. No data is written to existing runs.
* The ``model_endpoints`` table is created with ``IF NOT EXISTS`` guards
  so that repeated application is safe.
* No existing tables are modified.
* Phase 1–6 data is preserved.
"""

from alembic import op

revision = "0033_resource_governance"
down_revision = "0032_semantic_cache"
branch_labels = None
depends_on = None

ENDPOINT_STATUSES = ("healthy", "degraded", "unhealthy", "unknown")


def upgrade():
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS model_endpoints (
          endpoint_name             text NOT NULL PRIMARY KEY,
          url                       text NOT NULL DEFAULT '',
          status                    text NOT NULL DEFAULT 'unknown'
                                        CHECK (status IN {tuple(ENDPOINT_STATUSES)}),
          last_check_at             double precision,
          last_error                text,
          concurrent_requests       bigint NOT NULL DEFAULT 0,
          queued_requests           bigint NOT NULL DEFAULT 0,
          total_checks              bigint NOT NULL DEFAULT 0,
          total_failures            bigint NOT NULL DEFAULT 0,
          degraded_since            double precision,
          restart_count             bigint NOT NULL DEFAULT 0
        );
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'model_endpoints' AND indexname = 'idx_model_endpoints_status'
          ) THEN
            CREATE INDEX idx_model_endpoints_status
              ON model_endpoints (status);
          END IF;
        END $$;
        """
    )

    op.execute(
        "INSERT INTO schema_migrations(version) VALUES (33) ON CONFLICT DO NOTHING"
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v33 recovery boundary or apply a forward repair "
        "migration."
    )
