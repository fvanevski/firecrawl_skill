"""Bind every index job to the exact definition on its manifest."""

from alembic import op


revision = "0003_job_manifest_integrity"
down_revision = "0002_research_store_integrity"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        ALTER TABLE embedding_manifests
          ADD CONSTRAINT embedding_manifests_id_definition_key
          UNIQUE(id,index_definition_id);
        ALTER TABLE index_jobs
          DROP CONSTRAINT index_jobs_manifest_fk;
        ALTER TABLE index_jobs
          ADD CONSTRAINT index_jobs_manifest_definition_fk
          FOREIGN KEY(manifest_id,index_definition_id)
          REFERENCES embedding_manifests(id,index_definition_id)
          ON DELETE CASCADE;
        INSERT INTO schema_migrations(version) VALUES (3) ON CONFLICT DO NOTHING;
        """
    )


def downgrade():
    raise RuntimeError(
        "Research corpus migrations are forward-only; restore PostgreSQL from backup"
    )
