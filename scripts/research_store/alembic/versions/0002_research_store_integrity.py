"""Research store integrity, derivation versioning, and leased indexing.

This revision deliberately preserves all v1 corpus rows.  Existing embedding
manifests are rebound to immutable, fingerprinted physical collections and
queued for rebuilding because a v1 collection could not prove its model
provenance.
"""

from alembic import op


revision = "0002_research_store_integrity"
down_revision = "0001_research_store"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        DO $$
        DECLARE constraint_name text;
        BEGIN
          FOR constraint_name IN
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'relations'::regclass
              AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%relation_class%extraction_model%'
          LOOP
            EXECUTE format('ALTER TABLE relations DROP CONSTRAINT %I', constraint_name);
          END LOOP;
        END $$;
        UPDATE relations
        SET extraction_model='legacy-unknown',
            metadata=metadata || '{"migration_warning":"missing v1 model provenance"}'::jsonb
        WHERE relation_class='model_inferred' AND extraction_model IS NULL;
        UPDATE relations
        SET metadata=metadata || jsonb_build_object(
              'migration_warning','removed model provenance from non-inferred v1 relation',
              'legacy_extraction_model',extraction_model),
            extraction_model=NULL, extraction_version=NULL
        WHERE relation_class IN ('observed','source_asserted') AND extraction_model IS NOT NULL;
        ALTER TABLE relations ADD CONSTRAINT relations_model_provenance_check
          CHECK (
            (relation_class = 'model_inferred' AND extraction_model IS NOT NULL)
            OR
            (relation_class IN ('observed', 'source_asserted') AND extraction_model IS NULL)
          ) NOT VALID;
        ALTER TABLE relations VALIDATE CONSTRAINT relations_model_provenance_check;

        ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_snapshot_id_key;
        ALTER TABLE documents ADD CONSTRAINT documents_derivation_key
          UNIQUE(snapshot_id, parser_name, parser_version, normalization_version, document_sha256);

        ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_document_id_chunker_version_ordinal_key;
        ALTER TABLE chunks ADD CONSTRAINT chunks_derivation_key
          UNIQUE(document_id, chunker_name, chunker_version, ordinal);

        CREATE TABLE index_definitions(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          fingerprint text UNIQUE NOT NULL,
          physical_collection text UNIQUE NOT NULL,
          model_name text NOT NULL,
          model_revision text NOT NULL DEFAULT '',
          dimension integer NOT NULL CHECK(dimension > 0),
          distance_metric text NOT NULL,
          normalization text NOT NULL DEFAULT '',
          instruction_template_hash text NOT NULL DEFAULT '',
          lifecycle_status text NOT NULL DEFAULT 'building'
            CHECK(lifecycle_status IN ('building','active','inactive','failed')),
          created_at timestamptz NOT NULL DEFAULT now(),
          activated_at timestamptz
        );

        UPDATE embedding_manifests SET normalization='unit-length'
        WHERE normalization IS NULL OR normalization='';
        WITH definitions AS (
          SELECT DISTINCT
            encode(digest(
              '{"dimension":' || dimension::text ||
              ',"distance":' || to_json(distance_metric)::text ||
              ',"instruction_template_hash":' || to_json(instruction_template_hash)::text ||
              ',"model":' || to_json(model_name)::text ||
              ',"normalization":' || to_json(normalization)::text ||
              ',"revision":' || to_json(model_revision)::text || '}',
              'sha256'), 'hex') AS fingerprint,
            model_name, model_revision, dimension, distance_metric,
            coalesce(normalization,'') AS normalization,
            instruction_template_hash
          FROM embedding_manifests
        )
        INSERT INTO index_definitions(
          fingerprint, physical_collection, model_name, model_revision,
          dimension, distance_metric, normalization, instruction_template_hash
        )
        SELECT fingerprint, 'research_chunks_' || left(fingerprint, 12),
          model_name, model_revision, dimension, distance_metric,
          normalization, instruction_template_hash
        FROM definitions
        ON CONFLICT(fingerprint) DO NOTHING;

        ALTER TABLE embedding_manifests ADD COLUMN index_definition_id uuid;
        UPDATE embedding_manifests em
        SET index_definition_id = d.id,
            qdrant_collection = d.physical_collection,
            index_status = 'pending', indexed_at = NULL,
            error = 'rebuild required after v2 provenance migration'
        FROM index_definitions d
        WHERE d.fingerprint = encode(digest(
          '{"dimension":' || em.dimension::text ||
          ',"distance":' || to_json(em.distance_metric)::text ||
          ',"instruction_template_hash":' || to_json(em.instruction_template_hash)::text ||
          ',"model":' || to_json(em.model_name)::text ||
          ',"normalization":' || to_json(em.normalization)::text ||
          ',"revision":' || to_json(em.model_revision)::text || '}',
          'sha256'), 'hex');
        ALTER TABLE embedding_manifests ALTER COLUMN index_definition_id SET NOT NULL;
        ALTER TABLE embedding_manifests ADD CONSTRAINT embedding_manifests_index_definition_fk
          FOREIGN KEY(index_definition_id) REFERENCES index_definitions(id);
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
            EXECUTE format('ALTER TABLE embedding_manifests DROP CONSTRAINT %I', constraint_name);
          END LOOP;
        END $$;
        ALTER TABLE embedding_manifests ADD CONSTRAINT embedding_manifests_definition_key
          UNIQUE(chunk_id, index_definition_id);

        ALTER TABLE research_runs ADD COLUMN external_run_id text UNIQUE;
        ALTER TABLE research_runs ADD COLUMN outcome text;
        ALTER TABLE research_runs ADD COLUMN catalog_pointer text;
        ALTER TABLE research_runs ADD COLUMN source_manifest_sha256 text;
        ALTER TABLE research_runs ADD COLUMN answer_sha256 text;

        CREATE TABLE ingestion_batches(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          invocation_id text UNIQUE NOT NULL,
          operation text NOT NULL,
          research_run_id uuid REFERENCES research_runs(id),
          status text NOT NULL DEFAULT 'running'
            CHECK(status IN ('running','complete','partial','failed')),
          metadata jsonb NOT NULL DEFAULT '{}',
          started_at timestamptz NOT NULL DEFAULT now(),
          completed_at timestamptz,
          error text
        );
        CREATE TABLE ingestion_batch_assets(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          batch_id uuid NOT NULL REFERENCES ingestion_batches(id) ON DELETE CASCADE,
          ordinal integer NOT NULL,
          requested_url text NOT NULL,
          status text NOT NULL CHECK(status IN ('complete','failed')),
          source_id uuid REFERENCES sources(id),
          snapshot_id uuid REFERENCES asset_snapshots(id),
          document_id uuid REFERENCES documents(id),
          chunk_ids uuid[] NOT NULL DEFAULT '{}',
          error text,
          metadata jsonb NOT NULL DEFAULT '{}',
          UNIQUE(batch_id, ordinal)
        );
        CREATE INDEX ingestion_batch_assets_snapshot_idx
          ON ingestion_batch_assets(snapshot_id);

        CREATE TABLE research_run_assets(
          run_id uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
          snapshot_id uuid NOT NULL REFERENCES asset_snapshots(id),
          role text NOT NULL DEFAULT 'acquired',
          created_at timestamptz NOT NULL DEFAULT now(),
          metadata jsonb NOT NULL DEFAULT '{}',
          PRIMARY KEY(run_id, snapshot_id, role)
        );

        ALTER TABLE index_jobs ADD COLUMN manifest_id uuid;
        ALTER TABLE index_jobs ADD COLUMN index_definition_id uuid;
        ALTER TABLE index_jobs ADD COLUMN lease_token uuid;
        ALTER TABLE index_jobs ADD COLUMN lease_owner text;
        ALTER TABLE index_jobs ADD COLUMN lease_expires_at timestamptz;
        ALTER TABLE index_jobs ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();

        ALTER TABLE index_jobs DROP CONSTRAINT IF EXISTS index_jobs_entity_type_entity_id_index_name_operation_key;
        DELETE FROM index_jobs;
        INSERT INTO index_jobs(
          entity_type, entity_id, index_name, operation, status,
          manifest_id, index_definition_id, error
        )
        SELECT 'chunk', em.chunk_id, d.physical_collection, 'upsert', 'pending',
          em.id, d.id, 'rebuild required after v2 provenance migration'
        FROM embedding_manifests em
        JOIN index_definitions d ON d.id = em.index_definition_id;
        ALTER TABLE index_jobs ALTER COLUMN manifest_id SET NOT NULL;
        ALTER TABLE index_jobs ALTER COLUMN index_definition_id SET NOT NULL;
        ALTER TABLE index_jobs ADD CONSTRAINT index_jobs_manifest_fk
          FOREIGN KEY(manifest_id) REFERENCES embedding_manifests(id) ON DELETE CASCADE;
        ALTER TABLE index_jobs ADD CONSTRAINT index_jobs_definition_fk
          FOREIGN KEY(index_definition_id) REFERENCES index_definitions(id);
        ALTER TABLE index_jobs ADD CONSTRAINT index_jobs_manifest_operation_key
          UNIQUE(manifest_id, operation);

        DROP INDEX IF EXISTS index_jobs_pending_idx;
        CREATE INDEX index_jobs_claimable_idx ON index_jobs(status, available_at, lease_expires_at)
          WHERE status IN ('pending','failed','running');
        CREATE INDEX index_jobs_definition_idx ON index_jobs(index_definition_id, status);

        CREATE TABLE index_worker_heartbeats(
          worker_id text PRIMARY KEY,
          heartbeat_at timestamptz NOT NULL DEFAULT now(),
          metadata jsonb NOT NULL DEFAULT '{}'
        );

        CREATE TABLE index_activation_journal(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          target_definition_id uuid NOT NULL REFERENCES index_definitions(id),
          previous_definition_id uuid REFERENCES index_definitions(id),
          action text NOT NULL CHECK(action IN ('activate','rollback')),
          status text NOT NULL DEFAULT 'prepared'
            CHECK(status IN ('prepared','switched','complete','failed')),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          error text
        );
        CREATE UNIQUE INDEX index_activation_one_open_idx
          ON index_activation_journal((true))
          WHERE status IN ('prepared','switched');

        INSERT INTO schema_migrations(version) VALUES (2) ON CONFLICT DO NOTHING;
        """
    )


def downgrade():
    raise RuntimeError(
        "Research corpus migrations are forward-only; restore PostgreSQL from backup"
    )
