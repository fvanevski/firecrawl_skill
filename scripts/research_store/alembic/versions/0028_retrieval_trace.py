"""Add retrieval execution ID and batch tracing (issue #52).

This migration introduces the ``retrieval_execution_id`` column to the
``retrieval_events`` table required by issue #52 (Retrieval candidate
and stage tracing).
"""

from alembic import op

revision = "0028_retrieval_trace"
down_revision = "0027_retrieval_executions"
branch_labels = None
depends_on = None


def upgrade():
    # ----------------------------------------------------------------
    # 1. Add retrieval_execution_id to retrieval_events
    # ----------------------------------------------------------------
    op.execute(
        """
        ALTER TABLE retrieval_events
        ADD COLUMN retrieval_execution_id uuid REFERENCES retrieval_executions(id) ON DELETE CASCADE;
        """
    )

    # ----------------------------------------------------------------
    # 2. Create index for the new column
    # ----------------------------------------------------------------
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_retrieval_events_execution
          ON retrieval_events (retrieval_execution_id);
        """
    )

    # ----------------------------------------------------------------
    # 3. Record migration
    # ----------------------------------------------------------------
    op.execute(
        "INSERT INTO schema_migrations(version) VALUES (28) ON CONFLICT DO NOTHING"
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v28 recovery boundary or apply a forward repair "
        "migration."
    )
