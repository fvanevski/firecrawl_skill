"""Add index_point_counts cache table.

This migration introduces a cache table for Qdrant point counts, avoiding
the need to scroll all points from Qdrant on every doctor or reconcile call.

## Schema changes

### index_point_counts

Tracks the verified Qdrant point count per index definition.

* ``index_definition_id`` — uuid PK, references ``index_definitions(id)``
* ``point_count`` — bigint, the number of points in the Qdrant collection
* ``last_verified_at`` — timestamptz, when the count was last verified

## Design decisions

* The table is upsert-only: ``ON CONFLICT(index_definition_id) DO UPDATE``.
* Point counts are written after ``index-build`` verifies coverage, and after
  ``index-activate`` verifies alias cutover.
* The cache is invalidated when a collection is dropped (``ensure_schema``
  drop-and-recreate path).
* Doctor and reconcile read from the cache; the live Qdrant scroll is still
  used for the missing/orphaned point-ID check.

## Compatibility

* Forward-only migration. No data is written to existing runs.
* The ``index_point_counts`` table is created with ``IF NOT EXISTS`` guards
  so that repeated application is safe.
* No existing tables are modified.
* Phase 1–6 data is preserved.
"""

from alembic import op

revision = "0035_index_point_counts"
down_revision = "0034_add_validation_stage"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS index_point_counts (
          index_definition_id uuid NOT NULL PRIMARY KEY
            REFERENCES index_definitions(id) ON DELETE CASCADE,
          point_count bigint NOT NULL DEFAULT 0,
          last_verified_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes
            WHERE tablename = 'index_point_counts'
              AND indexname = 'idx_index_point_counts_definition'
          ) THEN
            CREATE INDEX idx_index_point_counts_definition
              ON index_point_counts (index_definition_id);
          END IF;
        END $$;
        """
    )

    op.execute(
        "INSERT INTO schema_migrations(version) VALUES (35) ON CONFLICT DO NOTHING"
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v35 recovery boundary or apply a forward repair "
        "migration."
    )
