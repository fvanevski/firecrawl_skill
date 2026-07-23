"""Add catalog_import_tracking table for idempotent catalog import auditing.

This migration introduces the table required by issue #37
(Import and reconcile retained Catalog history). It records
each import attempt, its source Catalog root, and a per-record
mapping from Catalog v5 identifiers to PostgreSQL surrogate keys.

## Schema changes

### catalog_import_tracking

Tracks one import invocation and its per-record reconciliation results.

* ``id`` — UUID PK, ``gen_random_uuid()``
* ``import_run_id`` — domain-level UUID identifying one import session
* ``catalog_root`` — the Catalog v5 root directory scanned (text)
* ``source_state_sha256`` — SHA-256 hex digest of the Catalog root manifest
* ``status`` — constrained to ``catalog_import_status`` enum
* ``records_inserted`` — bigint, number of rows inserted
* ``records_skipped`` — bigint, number of already-imported records
* ``records_conflicting`` — bigint, number of conflicting records
* ``records_malformed`` — bigint, number of malformed records
* ``records_omitted`` — bigint, number of omitted records (missing assets)
* ``started_at`` — timestamptz, defaults to ``now()``
* ``completed_at`` — timestamptz (nullable)

Constraints:
* ``chk_catalog_import_tracking_status`` — enum constraint.
* ``chk_catalog_import_tracking_counts`` — non-negative counts.
* ``chk_catalog_import_tracking_source_state`` — 64-char hex SHA-256.

### catalog_import_record_map

Maps individual Catalog v5 records to their PostgreSQL counterparts.

* ``id`` — UUID PK, ``gen_random_uuid()``
* ``import_run_id`` — FK to ``catalog_import_tracking(id)`` ON DELETE CASCADE
* ``catalog_type`` — constrained to ``catalog_record_type`` enum
* ``catalog_id`` — the Catalog v5 identifier (e.g. ``fr_<32hex>``, ``fc_<32hex>``)
* ``postgresql_id`` — the PostgreSQL surrogate key (nullable for failed mappings)
* ``mapping_status`` — constrained to ``catalog_mapping_status`` enum
* ``conflict_detail`` — free-text conflict description (nullable)
* ``created_at`` — timestamptz, defaults to ``now()``

Constraints:
* ``uk_catalog_import_record_map_import_catalog`` — unique
  ``(import_run_id, catalog_type, catalog_id)`` for idempotent re-import.
* ``chk_catalog_import_record_type`` — enum constraint.
* ``chk_catalog_import_record_status`` — enum constraint.

Indexes:
* ``idx_catalog_import_record_map_catalog`` — filter by ``catalog_type, catalog_id``
* ``idx_catalog_import_record_map_import`` — filter by ``import_run_id``
* ``idx_catalog_import_record_map_status`` — filter by ``mapping_status``

## Import/export behavior

* ``catalog-import`` — scans a Catalog root, validates records, and
  produces a reconciliation report. With ``--apply``, inserts records
  into PostgreSQL.
* ``catalog-reconcile`` — queries the import tracking table to
  produce a summary report of past imports and their reconciliation.

## Idempotency

The ``catalog_import_record_map`` unique constraint on
``(import_run_id, catalog_type, catalog_id)`` ensures that repeated
imports of the same Catalog root produce a clean report without
duplicating data. A second dry-run against the same root produces
the same report. A second apply against the same root is a no-op.

## Forward-repair

If this migration is interrupted, re-run ``upgrade head`` from the
last successful revision. The migration is idempotent — tables and enums
use ``CREATE TABLE IF NOT EXISTS`` and ``CREATE TYPE ... NOT EXISTS`` guards.

## Downgrade

Migrations are forward-only. Restore PostgreSQL from the pre-v20
recovery boundary or apply a forward-repair migration.
"""

from alembic import op


revision = "0020_catalog_import_tracking"
down_revision = "0019_audit_identity"
branch_labels = None
depends_on = None


def upgrade():
    # ----------------------------------------------------------------
    # 1. Create the catalog_import_status enum
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_type WHERE typname = 'catalog_import_status'
          ) THEN
            CREATE TYPE catalog_import_status AS ENUM (
              'running',
              'completed',
              'partial',
              'failed'
            );
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 2. Create the catalog_record_type enum
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_type WHERE typname = 'catalog_record_type'
          ) THEN
            CREATE TYPE catalog_record_type AS ENUM (
              'run',
              'invocation',
              'event',
              'claim',
              'assessment'
            );
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 3. Create the catalog_mapping_status enum
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_type WHERE typname = 'catalog_mapping_status'
          ) THEN
            CREATE TYPE catalog_mapping_status AS ENUM (
              'inserted',
              'skipped',
              'conflict',
              'malformed',
              'omitted',
              'pending'
            );
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 4. Create catalog_import_tracking table
    # ----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_import_tracking (
          id                          uuid NOT NULL DEFAULT gen_random_uuid(),
          import_run_id               uuid NOT NULL,
          catalog_root                text NOT NULL,
          source_state_sha256         text,
          status                      catalog_import_status NOT NULL DEFAULT 'running',
          records_inserted            bigint NOT NULL DEFAULT 0,
          records_skipped             bigint NOT NULL DEFAULT 0,
          records_conflicting         bigint NOT NULL DEFAULT 0,
          records_malformed           bigint NOT NULL DEFAULT 0,
          records_omitted             bigint NOT NULL DEFAULT 0,
          started_at                  timestamptz NOT NULL DEFAULT now(),
          completed_at                timestamptz,

          PRIMARY KEY (id),
          CONSTRAINT chk_catalog_import_tracking_status
            CHECK (status IS NOT NULL),
          CONSTRAINT chk_catalog_import_tracking_counts
            CHECK (
              records_inserted >= 0
              AND records_skipped >= 0
              AND records_conflicting >= 0
              AND records_malformed >= 0
              AND records_omitted >= 0
            ),
          CONSTRAINT chk_catalog_import_tracking_source_state
            CHECK (
              source_state_sha256 IS NULL
              OR (
                length(source_state_sha256) = 64
                AND source_state_sha256 ~ '^[0-9a-f]{64}$'
              )
            )
        );
        """
    )

    # ----------------------------------------------------------------
    # 5. Create catalog_import_record_map table
    # ----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS catalog_import_record_map (
          id                          uuid NOT NULL DEFAULT gen_random_uuid(),
          import_run_id               uuid NOT NULL REFERENCES catalog_import_tracking(id) ON DELETE CASCADE,
          catalog_type                catalog_record_type NOT NULL,
          catalog_id                  text NOT NULL,
          postgresql_id               uuid,
          mapping_status              catalog_mapping_status NOT NULL DEFAULT 'pending',
          conflict_detail             text,
          created_at                  timestamptz NOT NULL DEFAULT now(),

          PRIMARY KEY (id),
          CONSTRAINT uk_catalog_import_record_map_import_catalog
            UNIQUE (import_run_id, catalog_type, catalog_id),
          CONSTRAINT chk_catalog_import_record_type
            CHECK (catalog_type IS NOT NULL),
          CONSTRAINT chk_catalog_import_record_status
            CHECK (mapping_status IS NOT NULL)
        );
        """
    )

    # ----------------------------------------------------------------
    # 6. Add indexes
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'catalog_import_tracking' AND indexname = 'idx_catalog_import_tracking_run'
          ) THEN
            CREATE INDEX idx_catalog_import_tracking_run
              ON catalog_import_tracking (import_run_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'catalog_import_tracking' AND indexname = 'idx_catalog_import_tracking_status'
          ) THEN
            CREATE INDEX idx_catalog_import_tracking_status
              ON catalog_import_tracking (status);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'catalog_import_tracking' AND indexname = 'idx_catalog_import_tracking_source_state'
          ) THEN
            CREATE INDEX idx_catalog_import_tracking_source_state
              ON catalog_import_tracking (source_state_sha256);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'catalog_import_record_map' AND indexname = 'idx_catalog_import_record_map_catalog'
          ) THEN
            CREATE INDEX idx_catalog_import_record_map_catalog
              ON catalog_import_record_map (catalog_type, catalog_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'catalog_import_record_map' AND indexname = 'idx_catalog_import_record_map_import'
          ) THEN
            CREATE INDEX idx_catalog_import_record_map_import
              ON catalog_import_record_map (import_run_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'catalog_import_record_map' AND indexname = 'idx_catalog_import_record_map_status'
          ) THEN
            CREATE INDEX idx_catalog_import_record_map_status
              ON catalog_import_record_map (mapping_status);
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 7. Record migration
    # ----------------------------------------------------------------
    op.execute(
        "INSERT INTO schema_migrations(version) VALUES (20) ON CONFLICT DO NOTHING"
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v20 recovery boundary or apply a forward repair "
        "migration."
    )
