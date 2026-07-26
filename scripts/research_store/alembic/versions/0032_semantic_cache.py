"""Add semantic_cache table for versioned semantic-result caching.

This migration introduces the schema required by issue #41.

## Schema changes

### semantic_cache

Stores cached semantic-stage outputs with deterministic key identity.
Each entry is identified by a SHA-256 key hash derived from:

* semantic stage (outline, binding, draft, citation_pass),
* prompt version and hash,
* output schema version,
* model fingerprint (model name + revision + endpoint alias),
* input hash (hash of the structured input payload),
* policy version,
* relevant configuration.

* ``id`` — UUID PK, ``gen_random_uuid()``
* ``key_hash`` — text (SHA-256 hex digest), unique
* ``stage`` — one of: outline, binding, draft, citation_pass
* ``model_fingerprint`` — model name + revision + endpoint alias
* ``input_hash`` — SHA-256 hex digest of the structured input
* ``prompt_hash`` — SHA-256 hex digest of the prompt content
* ``prompt_version`` — the prompt template version
* ``schema_version`` — the output schema version
* ``policy_version`` — the budget policy version (nullable)
* ``configuration_hash`` — hash of the configuration dict (nullable)
* ``artifact`` — JSONB blob containing the cached output
* ``provenance`` — JSONB metadata about the original call
* ``status`` — one of: valid, expired, pruned
* ``ttl_seconds`` — time-to-live in seconds
* ``created_at`` — timestamptz (Unix timestamp stored as float)

Constraints:
* ``uk_semantic_cache_key`` — unique key_hash ensures no duplicate entries.
* ``ck_semantic_cache_status`` — status must be one of: valid, expired, pruned.
* ``ck_semantic_cache_stage`` — stage must be one of: outline, binding, draft, citation_pass.

## Design decisions

* The cache is append-only in spirit: each key_hash has exactly one row.
  Updates change status (valid → expired, valid → pruned) but never
  mutate the artifact.
* ``artifact`` and ``provenance`` are stored as JSONB for flexibility.
* ``created_at`` is a Unix timestamp (float) rather than timestamptz to
  simplify TTL expiration checks.
* TTL is enforced at the application level; the ``ttl_seconds`` column
  allows per-entry TTL configuration.
* Cache loss (expired, pruned, or missing entries) affects performance
  only — it never causes loss of authoritative workflow state.

## Compatibility

* Forward-only migration. No data is written to existing runs.
* The ``semantic_cache`` table is created with ``IF NOT EXISTS`` guards
  so that repeated application is safe.
* No existing tables are modified.
* Phase 1–6 data is preserved.
"""

from alembic import op

revision = "0032_semantic_cache"
down_revision = "0031_synthesis_stages"
branch_labels = None
depends_on = None

CACHE_STATUSES = ("valid", "expired", "pruned")
CACHE_STAGES = ("outline", "binding", "draft", "citation_pass")


def upgrade():
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS semantic_cache (
          id                          uuid NOT NULL DEFAULT gen_random_uuid(),
          key_hash                    text NOT NULL,
          stage                       text NOT NULL CHECK (stage IN {tuple(CACHE_STAGES)}),
          model_fingerprint           text NOT NULL,
          input_hash                  text NOT NULL,
          prompt_hash                 text NOT NULL,
          prompt_version              text NOT NULL,
          schema_version              bigint NOT NULL,
          policy_version              text,
          configuration_hash          text,
          artifact                    jsonb,
          provenance                  jsonb,
          status                      text NOT NULL DEFAULT 'valid'
                                        CHECK (status IN {tuple(CACHE_STATUSES)}),
          ttl_seconds                 bigint NOT NULL DEFAULT 3600,
          created_at                  double precision NOT NULL,

          PRIMARY KEY (id),
          CONSTRAINT uk_semantic_cache_key
            UNIQUE (key_hash)
        );
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'semantic_cache' AND indexname = 'idx_semantic_cache_stage'
          ) THEN
            CREATE INDEX idx_semantic_cache_stage
              ON semantic_cache (stage);
          END IF;
        END $$;
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'semantic_cache' AND indexname = 'idx_semantic_cache_status'
          ) THEN
            CREATE INDEX idx_semantic_cache_status
              ON semantic_cache (status);
          END IF;
        END $$;
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'semantic_cache' AND indexname = 'idx_semantic_cache_created'
          ) THEN
            CREATE INDEX idx_semantic_cache_created
              ON semantic_cache (created_at);
          END IF;
        END $$;
        """
    )

    op.execute(
        "INSERT INTO schema_migrations(version) VALUES (32) ON CONFLICT DO NOTHING"
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v32 recovery boundary or apply a forward repair "
        "migration."
    )
