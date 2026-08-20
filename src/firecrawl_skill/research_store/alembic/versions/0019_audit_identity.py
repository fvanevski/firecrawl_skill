"""Add target-scoped audit identity for completed assessments.

The datastore is initialized from a clean PostgreSQL schema, so this revision
only establishes the current identity columns and constraints; no historical
filesystem or pre-authority rows are imported or backfilled.
"""

from alembic import op

revision = "0019_audit_identity"
down_revision = "0018_audit_assessments"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        ALTER TABLE audit_assessments
          ADD COLUMN audit_identity_hash text;

        ALTER TABLE audit_assessments
          DROP CONSTRAINT IF EXISTS uk_audit_assessments_target,
          ALTER COLUMN model_fingerprint SET NOT NULL,
          ALTER COLUMN audit_identity_hash SET NOT NULL,
          ADD CONSTRAINT chk_audit_assessments_model_fingerprint
            CHECK (length(trim(model_fingerprint)) > 0),
          ADD CONSTRAINT chk_audit_assessments_audit_identity_hash
            CHECK (
              length(audit_identity_hash) = 64
              AND audit_identity_hash ~ '^[0-9a-f]{64}$'
            );

        CREATE UNIQUE INDEX uk_audit_assessments_completed_identity
          ON audit_assessments (
            run_id, target_type, target_id, audit_identity_hash
          )
          WHERE status = 'completed';
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; reset the disposable "
        "PostgreSQL datastore and reapply the current schema."
    )
