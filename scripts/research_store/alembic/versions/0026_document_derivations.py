"""Add document_derivations table for rederive v2 (issue #47).

This migration introduces the ``document_derivations`` table required
by issue #47 (Extend rederive and compatibility import for new parsers
and chunks). It tracks every rederive attempt per document, preserving
old derivations while recording new ones.

## Schema changes

### document_derivations

One row per rederive attempt.  Old derivations remain queryable; new
derivations are appended with ``pending`` status until activation.

* ``id`` — UUID PK, ``gen_random_uuid()``
* ``document_id`` — FK to ``documents(id)`` ON DELETE CASCADE
* ``snapshot_id`` — FK to ``asset_snapshots(id)`` ON DELETE CASCADE
  (the source snapshot this derivation was created from)
* ``status`` — constrained to ``derivation_status`` enum
* ``parser_version`` — parser version used for this derivation
* ``normalization_version`` — normalization version used
* ``chunker_name`` — chunker name used
* ``chunker_version`` — chunker version used
* ``tokenizer_name`` — tokenizer name used
* ``chunk_count`` — number of chunks produced (nullable)
* ``block_count`` — number of blocks produced (nullable)
* ``error_message`` — free-text error description (nullable)
* ``configuration_sha256`` — SHA-256 of the configuration dict used
  for deterministic deduplication
* ``created_at`` — timestamptz, defaults to ``now()``

Constraints:
* ``chk_document_derivations_status`` — valid status guard.
* ``chk_document_derivations_chunk_count`` — non-negative guard.
* ``chk_document_derivations_block_count`` — non-negative guard.
* ``chk_document_derivations_configuration_sha`` — 64-char hex SHA-256 guard.

Indexes:
* ``idx_document_derivations_document`` — filter by ``document_id``
* ``idx_document_derivations_snapshot`` — filter by ``snapshot_id``
* ``idx_document_derivations_status`` — filter by ``status``
* ``idx_document_derivations_configuration_sha`` — deduplication lookup
* ``idx_document_derivations_active`` — find active derivation per document

## Idempotency

Tables and enums use ``CREATE TABLE IF NOT EXISTS`` and
``CREATE INDEX IF NOT EXISTS`` guards.  Re-running ``upgrade head``
from the same point is a no-op.

## Forward-repair

If this migration is interrupted, re-run ``upgrade head`` from the
last successful revision.  The migration is idempotent.

## Downgrade

Migrations are forward-only. Restore PostgreSQL from the pre-v26
recovery boundary or apply a forward repair migration.

.. versionadded:: P5-07
   Introduced as part of rederive v2.
"""

from alembic import op


revision = "0026_document_derivations"
down_revision = "0025_hierarchical_chunks"
branch_labels = None
depends_on = None


def upgrade():
    # ----------------------------------------------------------------
    # 1. Create the derivation_status enum
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_type WHERE typname = 'derivation_status'
          ) THEN
            CREATE TYPE derivation_status AS ENUM (
              'pending',
              'active',
              'superseded',
              'failed'
            );
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 2. Create document_derivations table
    # ----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS document_derivations (
          id                          uuid NOT NULL DEFAULT gen_random_uuid(),
          document_id                 uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
          snapshot_id                 uuid NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
          status                      derivation_status NOT NULL DEFAULT 'pending',
          parser_version              text NOT NULL,
          normalization_version       text NOT NULL,
          chunker_name                text NOT NULL DEFAULT 'hierarchical',
          chunker_version             text NOT NULL,
          tokenizer_name              text NOT NULL DEFAULT 'cl100k_base',
          chunk_count                 int CHECK (chunk_count IS NULL OR chunk_count >= 0),
          block_count                 int CHECK (block_count IS NULL OR block_count >= 0),
          error_message               text,
          configuration_sha256        text,
          created_at                  timestamptz NOT NULL DEFAULT now(),

          PRIMARY KEY (id),
          CONSTRAINT chk_document_derivations_configuration_sha
            CHECK (
              configuration_sha256 IS NULL
              OR (
                length(configuration_sha256) = 64
                AND configuration_sha256 ~ '^[0-9a-f]{64}$'
              )
            )
        );
        """
    )

    # ----------------------------------------------------------------
    # 3. Create indexes for document_derivations
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'document_derivations' AND indexname = 'idx_document_derivations_document'
          ) THEN
            CREATE INDEX idx_document_derivations_document
              ON document_derivations (document_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'document_derivations' AND indexname = 'idx_document_derivations_snapshot'
          ) THEN
            CREATE INDEX idx_document_derivations_snapshot
              ON document_derivations (snapshot_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'document_derivations' AND indexname = 'idx_document_derivations_status'
          ) THEN
            CREATE INDEX idx_document_derivations_status
              ON document_derivations (status);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'document_derivations' AND indexname = 'idx_document_derivations_configuration_sha'
          ) THEN
            CREATE INDEX idx_document_derivations_configuration_sha
              ON document_derivations (configuration_sha256) WHERE configuration_sha256 IS NOT NULL;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'document_derivations' AND indexname = 'idx_document_derivations_active'
          ) THEN
            CREATE INDEX idx_document_derivations_active
              ON document_derivations (document_id, status) WHERE status IN ('pending', 'active');
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 4. Record migration
    # ----------------------------------------------------------------
    op.execute(
        "INSERT INTO schema_migrations(version) VALUES (26) ON CONFLICT DO NOTHING"
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v26 recovery boundary or apply a forward repair "
        "migration."
    )
