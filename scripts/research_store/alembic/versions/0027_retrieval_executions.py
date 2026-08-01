"""Add retrieval_executions table for execution contract (issue #51).

This migration introduces the ``retrieval_executions`` table required
by issue #51 (Retrieval execution and degradation contract). It tracks
the execution parameters, modes, and component health for each retrieval operation.
"""

from alembic import op

revision = "0027_retrieval_executions"
down_revision = "0026_document_derivations"
branch_labels = None
depends_on = None


def upgrade():
    # ----------------------------------------------------------------
    # 1. Create retrieval_executions table
    # ----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS retrieval_executions (
          id                          uuid NOT NULL DEFAULT gen_random_uuid(),
          run_id                      uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
          requested_mode              text NOT NULL,
          executed_mode               text NOT NULL,
          mechanical_status           text NOT NULL,
          component_health            jsonb NOT NULL,
          errors                      jsonb NOT NULL,
          warnings                    jsonb NOT NULL,
          stage_counts                jsonb NOT NULL,
          index_fingerprint           text,
          filters                     jsonb NOT NULL,
          skipped_stages              jsonb NOT NULL,
          timing                      jsonb NOT NULL,
          config_identity             text NOT NULL,
          created_at                  timestamptz NOT NULL DEFAULT now(),

          PRIMARY KEY (id)
        );
        """
    )

    # ----------------------------------------------------------------
    # 2. Create indexes for retrieval_executions
    # ----------------------------------------------------------------
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_retrieval_executions_run
          ON retrieval_executions (run_id);
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v27 recovery boundary or apply a forward repair "
        "migration."
    )
