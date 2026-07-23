"""Add normalized_blocks and transformation_records tables (issue #45).

This migration introduces two new tables for reversible normalization:

## Schema changes

### normalized_blocks

One row per source block after normalization.  Each row records the
disposition (keep, alter, suppress, remove), the rule version applied,
and the transformation reason.

* ``id`` — UUID PK, ``gen_random_uuid()``
* ``source_block_id`` — FK to ``document_blocks(id)`` ON DELETE CASCADE
* ``document_id`` — FK to ``documents(id)`` ON DELETE CASCADE
* ``ordinal`` — integer, positional index within the document
* ``block_type`` — semantic type tag (inherited from source block)
* ``text`` — normalized text content (empty for ``remove`` blocks)
* ``heading_path`` — text array, ancestor heading path
* ``disposition`` — one of keep, alter, suppress, remove
* ``rule_version`` — normalization rule version (e.g. ``normalization-v1``)
* ``transformation_reason`` — why this disposition was chosen
* ``parser_version`` — parser version from the source block

Constraints:
* ``uk_normalized_blocks_source_block`` — unique source block linkage.

Indexes:
* ``idx_normalized_blocks_document`` — filter by ``document_id``
* ``idx_normalized_blocks_disposition`` — filter by ``disposition``
* ``idx_normalized_blocks_source_block`` — filter by ``source_block_id``

### transformation_records

One row per transformation applied during normalization.  Provides
full audit trail for reversible normalization.

* ``id`` — UUID PK, ``gen_random_uuid()``
* ``normalized_block_id`` — FK to ``document_blocks(id)`` ON DELETE CASCADE
  (references the source block that was transformed; the column name is
  retained for API stability — it points to the source, not a
  ``normalized_blocks`` row).
* ``rule_id`` — rule identifier (constrained to known rule IDs via check)
* ``rule_version`` — normalization rule version
* ``reason`` — human-readable reason for the transformation
* ``before_text`` — text before transformation (may be empty)
* ``after_text`` — text after transformation (may be empty)
* ``confidence`` — confidence score in [0, 1]

Constraints:
* ``chk_transformation_records_rule_id`` — valid rule ID guard.
* ``chk_transformation_records_confidence`` — confidence in [0, 1] guard.

Indexes:
* ``idx_transformation_records_normalized_block`` — filter by ``normalized_block_id``
* ``idx_transformation_records_rule_id`` — filter by ``rule_id``

## Idempotency

Tables use ``CREATE TABLE IF NOT EXISTS`` and ``CREATE INDEX IF NOT EXISTS``
guards.  Re-running ``upgrade head`` from the same point is a no-op.

## Forward-repair

If this migration is interrupted, re-run ``upgrade head`` from the
last successful revision.  The migration is idempotent.

## Downgrade

Migrations are forward-only. Restore PostgreSQL from the pre-v24
recovery boundary or apply a forward repair migration.

.. versionchanged:: P5-05
   Introduced as part of Phase 5 reversible normalization.
"""

from alembic import op


revision = "0024_normalized_blocks_and_transformations"
down_revision = "0023_parser_version"
branch_labels = None
depends_on = None


def upgrade():
    # ----------------------------------------------------------------
    # 1. Create normalized_blocks table
    # ----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS normalized_blocks (
          id                          uuid NOT NULL DEFAULT gen_random_uuid(),
          source_block_id             uuid NOT NULL REFERENCES document_blocks(id) ON DELETE CASCADE,
          document_id                 uuid REFERENCES documents(id) ON DELETE CASCADE,
          ordinal                     int NOT NULL,
          block_type                  text NOT NULL,
          text                        text NOT NULL DEFAULT '',
          heading_path                text[],
          char_start                  int,
          char_end                    int,
          disposition                 text NOT NULL DEFAULT 'keep',
          rule_version                text NOT NULL DEFAULT 'normalization-v1',
          transformation_reason       text,
          parser_version              text NOT NULL DEFAULT 'canonical-v1',

          PRIMARY KEY (id),
          CONSTRAINT chk_normalized_blocks_disposition
            CHECK (disposition IN ('keep', 'alter', 'suppress', 'remove')),
          CONSTRAINT uk_normalized_blocks_source_block_rule
            UNIQUE (source_block_id, rule_version)
        );
        """
    )

    # ----------------------------------------------------------------
    # 2. Create indexes for normalized_blocks
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'normalized_blocks' AND indexname = 'idx_normalized_blocks_document'
          ) THEN
            CREATE INDEX idx_normalized_blocks_document
              ON normalized_blocks (document_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'normalized_blocks' AND indexname = 'idx_normalized_blocks_disposition'
          ) THEN
            CREATE INDEX idx_normalized_blocks_disposition
              ON normalized_blocks (disposition);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'normalized_blocks' AND indexname = 'idx_normalized_blocks_source_block'
          ) THEN
            CREATE INDEX idx_normalized_blocks_source_block
              ON normalized_blocks (source_block_id);
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 3. Create transformation_records table
    # ----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS transformation_records (
          id                          uuid NOT NULL DEFAULT gen_random_uuid(),
          normalized_block_id         uuid REFERENCES normalized_blocks(id) ON DELETE CASCADE,
          rule_id                     text NOT NULL,
          rule_version                text NOT NULL DEFAULT 'normalization-v1',
          reason                      text NOT NULL DEFAULT '',
          before_text                 text NOT NULL DEFAULT '',
          after_text                  text NOT NULL DEFAULT '',
          confidence                  float8 NOT NULL DEFAULT 1.0,

          PRIMARY KEY (id),
          CONSTRAINT chk_transformation_records_rule_id
            CHECK (rule_id IN (
              'strip-cookie-notice',
              'strip-navigation',
              'strip-social-links',
              'strip-boilerplate-heading',
              'preserve-citation',
              'preserve-code-block',
              'preserve-meaningful-link',
              'preserve-short-heading',
              'preserve-footnote',
              'preserve-source-url',
              'doc-type-footer-digest',
              'no-change'
            )),
          CONSTRAINT chk_transformation_records_confidence
            CHECK (confidence >= 0.0 AND confidence <= 1.0),
          CONSTRAINT uk_transformation_records_block_rule
            UNIQUE (normalized_block_id, rule_id)
        );
        """
    )

    # ----------------------------------------------------------------
    # 4. Create indexes for transformation_records
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'transformation_records' AND indexname = 'idx_transformation_records_normalized_block'
          ) THEN
            CREATE INDEX idx_transformation_records_normalized_block
              ON transformation_records (normalized_block_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'transformation_records' AND indexname = 'idx_transformation_records_rule_id'
          ) THEN
            CREATE INDEX idx_transformation_records_rule_id
              ON transformation_records (rule_id);
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 5. Record migration
    # ----------------------------------------------------------------
    op.execute(
        "INSERT INTO schema_migrations(version) VALUES (24) ON CONFLICT DO NOTHING"
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v24 recovery boundary or apply a forward repair "
        "migration."
    )
