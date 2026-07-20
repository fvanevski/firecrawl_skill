"""Remove the stale v1 embedding-manifest uniqueness constraint."""

from alembic import op


revision = "0004_drop_legacy_manifest_key"
down_revision = "0003_job_manifest_integrity"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        DO $$
        DECLARE constraint_name text;
        BEGIN
          FOR constraint_name IN
            SELECT c.conname
            FROM pg_constraint c
            WHERE c.conrelid='embedding_manifests'::regclass
              AND c.contype='u'
              AND ARRAY(
                SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY key(attnum,ord)
                JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=key.attnum
                ORDER BY key.ord
              ) = ARRAY['chunk_id','model_name','model_revision','instruction_template_hash']::text[]
          LOOP
            EXECUTE format(
              'ALTER TABLE embedding_manifests DROP CONSTRAINT %I', constraint_name
            );
          END LOOP;
        END $$;
        INSERT INTO schema_migrations(version) VALUES (4) ON CONFLICT DO NOTHING;
        """
    )


def downgrade():
    raise RuntimeError(
        "Research corpus migrations are forward-only; restore PostgreSQL from backup"
    )
