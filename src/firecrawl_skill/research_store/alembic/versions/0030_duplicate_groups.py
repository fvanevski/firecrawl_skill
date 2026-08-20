"""Add duplicate_groups table and independence_assessment column.

This migration introduces the schema changes required by issue #57.

## Schema changes

### duplicate_groups
Stores grouping information for near-duplicate search candidates.

* ``id`` — UUID PK, ``gen_random_uuid()``
* ``run_id`` — FK to ``research_runs(id)`` ON DELETE CASCADE
* ``rationale`` — text rationale explaining the grouping
* ``created_at`` — timestamptz, defaults to ``now()``

### search_candidates (modifications)
* Add FK from ``duplicate_group_id`` to ``duplicate_groups(id)``
* Add ``independence_assessment`` JSONB column (defaults to '{}')
"""

from alembic import op

revision = "0030_duplicate_groups"
down_revision = "0029_evidence_packets"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Create duplicate_groups table with (id, run_id) unique constraint
    #    so that the FK from search_candidates can be composite.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS duplicate_groups (
          id                          uuid NOT NULL DEFAULT gen_random_uuid(),
          run_id                      uuid REFERENCES research_runs(id) ON DELETE CASCADE,
          rationale                   text NOT NULL,
          created_at                  timestamptz NOT NULL DEFAULT now(),

          PRIMARY KEY (id),
          CONSTRAINT uk_duplicate_groups_run UNIQUE (id, run_id)
        );
        """
    )

    # 2. Add foreign key constraint to search_candidates (composite FK)
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'fk_search_candidates_duplicate_group'
          ) THEN
            ALTER TABLE search_candidates
              ADD CONSTRAINT fk_search_candidates_duplicate_group
              FOREIGN KEY (duplicate_group_id, run_id)
              REFERENCES duplicate_groups(id, run_id) ON DELETE SET NULL;
          END IF;
        END $$;
        """
    )

    # 3. Add independence_assessment to search_candidates
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns 
            WHERE table_name='search_candidates' AND column_name='independence_assessment'
          ) THEN
            ALTER TABLE search_candidates ADD COLUMN independence_assessment jsonb NOT NULL DEFAULT '{}';
          END IF;
        END $$;
        """
    )

    # 4. Add index for duplicate_groups.run_id
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'duplicate_groups' AND indexname = 'idx_duplicate_groups_run'
          ) THEN
            CREATE INDEX idx_duplicate_groups_run
              ON duplicate_groups (run_id);
          END IF;
        END $$;
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v30 recovery boundary or apply a forward repair "
        "migration."
    )
