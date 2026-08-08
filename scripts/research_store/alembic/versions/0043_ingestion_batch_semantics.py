"""Add authoritative ingestion batch timing, outcomes, and seal gating.

RC-11 / RC-12 / RC-13 tracked by #217.

New rows use three distinct temporal concepts:

* ``started_at`` — earliest authoritative constituent start;
* ``sealed_at`` — membership closure time; and
* ``completed_at`` — latest authoritative constituent terminal outcome.

``ingestion_batch_assets`` stores a direct ``extraction_attempt_id`` whenever a
batch member belongs to an extraction attempt.  Direct-ingestion members instead
persist ``constituent_started_at`` / ``constituent_completed_at``.  Batch
finalization fails closed when any new member lacks its authoritative timing
source; no statement-time completion fallback is used for v43 batches.

``outcome_summary`` stores deterministic counts and stable batch-member IDs for
succeeded, failed, and cancelled outcomes plus per-failure-class IDs/counts.
Cancellation is derived from ``extraction_attempts.exit_status`` rather than
being overloaded onto the ingestion asset status check constraint.

No historical timestamps or promotion intent are invented. Existing rows retain
their prior ``started_at`` / ``completed_at``; new columns default to NULL (or an
empty summary) until authoritative new work populates them.
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
          ADD COLUMN constituent_started_at timestamptz;

        ALTER TABLE ingestion_batch_assets
          ADD COLUMN constituent_completed_at timestamptz;

        ALTER TABLE ingestion_batch_assets
          ADD CONSTRAINT ingestion_batch_assets_extr_att_fk
            FOREIGN KEY (extraction_attempt_id)
            REFERENCES extraction_attempts(id) ON DELETE SET NULL;

        ALTER TABLE ingestion_batch_assets
          ADD CONSTRAINT ingestion_batch_assets_constituent_timing_ck
            CHECK (
              constituent_completed_at IS NULL
              OR constituent_started_at IS NULL
              OR constituent_completed_at >= constituent_started_at
            );

        CREATE INDEX ix_ingestion_batch_assets_extraction_attempt
          ON ingestion_batch_assets(extraction_attempt_id)
          WHERE extraction_attempt_id IS NOT NULL;
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; apply a forward repair "
        "for ingestion batch semantics or restore PostgreSQL from backup"
    )
