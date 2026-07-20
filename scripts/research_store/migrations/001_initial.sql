CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS schema_migrations(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS sources(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), canonical_url text UNIQUE NOT NULL,
 registered_domain text, source_type text, first_seen_at timestamptz NOT NULL DEFAULT now(),
 last_seen_at timestamptz NOT NULL DEFAULT now(), default_authority_class text, metadata jsonb NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS sources_domain_idx ON sources(registered_domain);
CREATE INDEX IF NOT EXISTS sources_metadata_idx ON sources USING gin(metadata);

CREATE TABLE IF NOT EXISTS asset_snapshots(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), source_id uuid NOT NULL REFERENCES sources(id), requested_url text NOT NULL,
 final_url text, retrieved_at timestamptz NOT NULL, http_status integer, etag text, last_modified text, mime_type text,
 content_sha256 text NOT NULL, raw_blob_uri text, raw_byte_length bigint, firecrawl_version text,
 crawl_options jsonb NOT NULL DEFAULT '{}', parent_snapshot_id uuid REFERENCES asset_snapshots(id),
 UNIQUE(source_id, content_sha256));
CREATE INDEX IF NOT EXISTS snapshots_source_idx ON asset_snapshots(source_id);
CREATE INDEX IF NOT EXISTS snapshots_retrieved_idx ON asset_snapshots(retrieved_at);
CREATE INDEX IF NOT EXISTS snapshots_content_hash_idx ON asset_snapshots(content_sha256);

CREATE TABLE IF NOT EXISTS documents(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), snapshot_id uuid NOT NULL UNIQUE REFERENCES asset_snapshots(id), title text,
 author text, published_at timestamptz, language text, normalized_markdown text, normalized_text text,
 parser_name text NOT NULL, parser_version text NOT NULL, normalization_version text NOT NULL,
 document_sha256 text NOT NULL, metadata jsonb NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS documents_snapshot_idx ON documents(snapshot_id);
CREATE INDEX IF NOT EXISTS documents_published_idx ON documents(published_at);
CREATE INDEX IF NOT EXISTS documents_hash_idx ON documents(document_sha256);
CREATE INDEX IF NOT EXISTS documents_metadata_idx ON documents USING gin(metadata);

CREATE TABLE IF NOT EXISTS document_blocks(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 parent_block_id uuid REFERENCES document_blocks(id), block_type text NOT NULL, heading_path text[] NOT NULL DEFAULT '{}',
 ordinal integer NOT NULL, char_start integer, char_end integer, text text NOT NULL, metadata jsonb NOT NULL DEFAULT '{}',
 UNIQUE(document_id, ordinal));
CREATE INDEX IF NOT EXISTS blocks_document_idx ON document_blocks(document_id);

CREATE TABLE IF NOT EXISTS chunks(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 first_block_id uuid REFERENCES document_blocks(id), last_block_id uuid REFERENCES document_blocks(id), ordinal integer NOT NULL,
 text text NOT NULL, token_count integer, content_sha256 text NOT NULL, chunker_name text NOT NULL,
 chunker_version text NOT NULL, metadata jsonb NOT NULL DEFAULT '{}',
 search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text,''))) STORED,
 UNIQUE(document_id, chunker_version, ordinal));
CREATE INDEX IF NOT EXISTS chunks_document_idx ON chunks(document_id);
CREATE INDEX IF NOT EXISTS chunks_hash_idx ON chunks(content_sha256);
CREATE INDEX IF NOT EXISTS chunks_fts_idx ON chunks USING gin(search_vector);

CREATE TABLE IF NOT EXISTS embedding_manifests(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), chunk_id uuid NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
 model_name text NOT NULL, model_revision text NOT NULL DEFAULT '', dimension integer NOT NULL, distance_metric text NOT NULL,
 normalization text, instruction_template_hash text NOT NULL DEFAULT '', qdrant_collection text, qdrant_point_id uuid,
 index_status text NOT NULL CHECK(index_status IN ('pending','indexing','complete','failed')),
 indexed_at timestamptz, error text, UNIQUE(chunk_id, model_name, model_revision, instruction_template_hash));

CREATE TABLE IF NOT EXISTS research_runs(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), original_request text NOT NULL, query_plan jsonb,
 skill_version text, llm_model text, retrieval_policy_version text, started_at timestamptz NOT NULL DEFAULT now(),
 completed_at timestamptz, status text NOT NULL, error text);
CREATE TABLE IF NOT EXISTS retrieval_events(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), run_id uuid NOT NULL REFERENCES research_runs(id), stage text NOT NULL,
 query text, filters jsonb, retriever text, candidate_type text, candidate_id uuid, raw_score double precision,
 normalized_score double precision, rank integer, reranker_score double precision, selected boolean,
 rejection_reason text, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS retrieval_events_run_idx ON retrieval_events(run_id, created_at);

CREATE TABLE IF NOT EXISTS index_jobs(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), entity_type text NOT NULL, entity_id uuid NOT NULL, index_name text NOT NULL,
 operation text NOT NULL, status text NOT NULL CHECK(status IN ('pending','running','complete','failed','dead')),
 attempt_count integer NOT NULL DEFAULT 0, available_at timestamptz NOT NULL DEFAULT now(), created_at timestamptz NOT NULL DEFAULT now(),
 started_at timestamptz, completed_at timestamptz, error text, UNIQUE(entity_type, entity_id, index_name, operation));
CREATE INDEX IF NOT EXISTS index_jobs_pending_idx ON index_jobs(status, available_at) WHERE status IN ('pending','failed');

CREATE TABLE IF NOT EXISTS relations(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), subject_type text NOT NULL, subject_id uuid NOT NULL, predicate text NOT NULL,
 object_type text NOT NULL, object_id uuid, object_literal text, relation_class text NOT NULL
 CHECK(relation_class IN ('observed','source_asserted','model_inferred')),
 source_snapshot_id uuid REFERENCES asset_snapshots(id), source_block_id uuid REFERENCES document_blocks(id),
 supporting_span text, extraction_model text, extraction_version text, confidence double precision,
 review_status text, metadata jsonb NOT NULL DEFAULT '{}',
 CHECK(object_id IS NOT NULL OR object_literal IS NOT NULL),
 CHECK(relation_class = 'model_inferred' OR extraction_model IS NULL));
CREATE INDEX IF NOT EXISTS relations_subject_idx ON relations(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS relations_object_idx ON relations(object_type, object_id);

INSERT INTO schema_migrations(version) VALUES (1) ON CONFLICT DO NOTHING;

