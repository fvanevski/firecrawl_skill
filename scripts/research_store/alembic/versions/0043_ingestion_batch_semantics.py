"""Add truthful batch timestamps, outcome summaries, and seal gating.

RC-11 / RC-12 / RC-13 tracked by #217.

* ``sealed_at`` — when the batch's membership closure was recorded.
* ``outcome_summary`` — JSONB blob capturing succeeded/failed/cancelled counts
  and per-class failure breakdown so operators can read partial status without
  joining every attempt manually.
* ``started_at`` is preserved as the wall-clock row creation time; callers must
  derive constituent-driven ``completed_at`` at finish time instead of using
  statement wall clock.

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
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; apply a forward repair "
        "for ingestion batch semantics or restore PostgreSQL from backup"
    )
