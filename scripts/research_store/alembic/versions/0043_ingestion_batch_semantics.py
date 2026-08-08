"""Add truthful batch timestamps, outcome summaries, and seal gating.

RC-11 / RC-12 / RC-13 tracked by #217.

* ``sealed_at`` — when the batch's membership closure was recorded.
* ``outcome_summary`` — JSONB blob capturing succeeded/failed/cancelled counts
  and per-class failure breakdown so operators can read partial status without
  joining every attempt manually.

``started_at`` is preserved as the wall-clock row creation time for historical
records; callers derive constituent-driven timing at finish time instead of
relying on statement wall clock. Batches created after this migration receive
constituent-derived ``started_at`` when the earliest extraction attempt start
is available.

No historical timestamps are invented. Existing rows keep their original
``started_at`` and ``completed_at`` values; ``sealed_at`` defaults to NULL and
``outcome_summary`` defaults to '{}'.
"""

from alembic import op

revision = "0043_ingestion_batch_semantics"
down_revision = "0042_candidate_ranking_budgets"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        ALTER TABLE ingestion_batches
          ADD COLUMN sealed_at timestamptz;

        ALTER TABLE ingestion_batches
          ADD COLUMN outcome_summary jsonb NOT NULL DEFAULT '{}'::jsonb;

        ALTER TABLE ingestion_batch_assets
          ADD COLUMN extraction_attempt_id uuid;

        ALTER TABLE ingestion_batch_assets
          ADD CONSTRAINT ingestion_batch_assets_extr_att_fk
            FOREIGN KEY (extraction_attempt_id)
            REFERENCES extraction_attempts(id) ON DELETE SET NULL;
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; apply a forward repair "
        "for ingestion batch semantics or restore PostgreSQL from backup"
    )
