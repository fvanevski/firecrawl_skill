"""Enforce terminal research-run lifecycle invariants."""

from alembic import op


revision = "0005_run_lifecycle"
down_revision = "0004_drop_legacy_manifest_key"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        UPDATE research_runs
        SET status='running',completed_at=NULL,outcome=NULL
        WHERE completed_at IS NULL;
        UPDATE research_runs
        SET status=CASE WHEN status='failed' THEN 'failed' ELSE 'complete' END,
            outcome=coalesce(outcome,
              CASE WHEN status='failed' THEN 'failed' ELSE 'legacy-unknown' END)
        WHERE completed_at IS NOT NULL;
        ALTER TABLE research_runs
          ADD CONSTRAINT research_runs_lifecycle_check
          CHECK (
            (status='running' AND completed_at IS NULL AND outcome IS NULL)
            OR
            (status IN ('complete','failed') AND completed_at IS NOT NULL
              AND outcome IS NOT NULL)
          ) NOT VALID;
        ALTER TABLE research_runs VALIDATE CONSTRAINT research_runs_lifecycle_check;
        INSERT INTO schema_migrations(version) VALUES (5) ON CONFLICT DO NOTHING;
        """
    )


def downgrade():
    raise RuntimeError(
        "Research corpus migrations are forward-only; restore PostgreSQL from backup"
    )
