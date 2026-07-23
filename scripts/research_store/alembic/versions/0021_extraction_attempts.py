"""Add extraction_attempts table for attempt provenance (issue #40).

This migration introduces the ``extraction_attempts`` table required
by issue #40 (Extraction-attempt schema and service). It records every
extraction method invocation, its output, failure, retry, and final
disposition.

## Schema changes

### extraction_attempts

One row per extraction method attempt.  Multiple attempts per
candidate are ordered by ``attempt_number``.

* ``id`` — UUID PK, ``gen_random_uuid()``
* ``candidate_id`` — FK to ``search_candidates(id)`` ON DELETE CASCADE
* ``run_id`` — FK to ``research_runs(id)`` ON DELETE CASCADE
* ``invocation_id`` — FK to ``research_invocation_events(id)`` ON DELETE SET NULL
* ``attempt_number`` — integer, >= 1, ordered per candidate
* ``method`` — constrained to ``extraction_method`` enum
* ``method_version`` — free-text implementation version
* ``requested_format`` — target output format (nullable)
* ``start_time`` — timestamptz, when extraction began
* ``end_time`` — timestamptz (nullable), when extraction ended
* ``exit_status`` — constrained to ``extraction_exit_status`` enum
* ``http_status`` — HTTP status code from backend (nullable)
* ``backend_status`` — backend-specific status (nullable)
* ``raw_blob_sha256`` — content-addressed digest of raw payload (nullable)
* ``raw_blob_uri`` — blob URI for raw payload (nullable)
* ``raw_blob_byte_length`` — byte length of raw payload (nullable)
* ``raw_blob_mime_type`` — MIME type of raw payload (nullable)
* ``normalized_blob_sha256`` — content-addressed digest of normalized artifact (nullable)
* ``normalized_blob_uri`` — blob URI for normalized artifact (nullable)
* ``normalized_blob_byte_length`` — byte length of normalized artifact (nullable)
* ``normalized_blob_mime_type`` — MIME type of normalized artifact (nullable)
* ``parser_used`` — parser version used (nullable)
* ``quality_metrics`` — JSONB, deterministic quality evaluation (nullable)
* ``failure_class`` — constrained to ``extraction_failure_class`` enum
* ``retry_parent_id`` — FK to ``extraction_attempts(id)`` ON DELETE SET NULL (nullable)
* ``disposition`` — constrained to ``extraction_disposition`` enum
* ``error_message`` — free-text error description (nullable)
* ``selection_reason`` — why this attempt was selected as final (nullable)
* ``selected`` — boolean, is this the currently selected final attempt?
* ``created_at`` — timestamptz, defaults to ``now()``

Constraints:
* ``chk_extraction_attempts_attempt_number`` — must be >= 1.
* ``chk_extraction_attempts_raw_blob_sha`` — 64-char hex SHA-256 guard.
* ``chk_extraction_attempts_normalized_blob_sha`` — 64-char hex SHA-256 guard.
* ``chk_extraction_attempts_quality_metrics`` — non-null JSONB guard.
* ``uk_extraction_attempts_candidate_attempt`` — unique ordering per candidate.

Indexes:
* ``idx_extraction_attempts_candidate`` — filter by ``candidate_id``
* ``idx_extraction_attempts_run`` — filter by ``run_id``
* ``idx_extraction_attempts_selected`` — filter by ``selected=true``
* ``idx_extraction_attempts_retry_parent`` — filter by ``retry_parent_id``
* ``idx_extraction_attempts_exit_status`` — filter by ``exit_status``
* ``idx_extraction_attempts_disposition`` — filter by ``disposition``
* ``idx_extraction_attempts_method`` — filter by ``method``

## Referential invariants

1. ``candidate_id`` must reference an existing ``search_candidates`` row.
2. ``run_id`` must reference an existing ``research_runs`` row.
3. ``invocation_id`` must reference an existing ``research_invocation_events`` row (or be NULL).
4. ``retry_parent_id`` must reference an existing ``extraction_attempts`` row (or be NULL).
5. A corpus snapshot derived from extraction must reference the selected attempt.

## Import/export behavior

* No CLI subcommand is added in this migration.  The ``research-db``
  CLI will gain ``extraction-attempt`` subcommands in a follow-up issue.

## Idempotency

Tables and enums use ``CREATE TABLE IF NOT EXISTS`` and
``CREATE TYPE ... NOT EXISTS`` guards.  Re-running ``upgrade head``
from the same point is a no-op.

## Forward-repair

If this migration is interrupted, re-run ``upgrade head`` from the
last successful revision.  The migration is idempotent.

## Downgrade

Migrations are forward-only. Restore PostgreSQL from the pre-v21
recovery boundary or apply a forward-repair migration.
"""

from alembic import op


revision = "0021_extraction_attempts"
down_revision = "0020_catalog_import_tracking"
branch_labels = None
depends_on = None


def upgrade():
    # ----------------------------------------------------------------
    # 1. Create the extraction_method enum
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_type WHERE typname = 'extraction_method'
          ) THEN
            CREATE TYPE extraction_method AS ENUM (
              'firecrawl_main_content',
              'firecrawl_full_page',
              'deterministic_html',
              'deterministic_markdown',
              'deterministic_json',
              'deterministic_plain_text',
              'browser_capable',
              'alternate_adapter',
              'structured_extraction',
              'semantic_adjudication'
            );
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 2. Create the extraction_exit_status enum
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_type WHERE typname = 'extraction_exit_status'
          ) THEN
            CREATE TYPE extraction_exit_status AS ENUM (
              'succeeded',
              'partial',
              'failed',
              'cancelled'
            );
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 3. Create the extraction_failure_class enum
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_type WHERE typname = 'extraction_failure_class'
          ) THEN
            CREATE TYPE extraction_failure_class AS ENUM (
              'none',
              'timeout',
              'network',
              'http_error',
              'parser',
              'schema_validation',
              'empty_content',
              'anti_bot',
              'unsupported_format',
              'blocked',
              'content_too_small',
              'content_too_large',
              'malformed',
              'internal'
            );
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 4. Create the extraction_disposition enum
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_type WHERE typname = 'extraction_disposition'
          ) THEN
            CREATE TYPE extraction_disposition AS ENUM (
              'acceptable',
              'poor',
              'ambiguous',
              'unassessed'
            );
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 5. Create extraction_attempts table
    # ----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS extraction_attempts (
          id                          uuid NOT NULL DEFAULT gen_random_uuid(),
          candidate_id                uuid NOT NULL REFERENCES search_candidates(id) ON DELETE CASCADE,
          run_id                      uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
          invocation_id               uuid REFERENCES research_invocation_events(id) ON DELETE SET NULL,
          attempt_number              int NOT NULL DEFAULT 1,
          method                      extraction_method NOT NULL,
          method_version              text NOT NULL,
          requested_format            text,
          start_time                  timestamptz NOT NULL,
          end_time                    timestamptz,
          exit_status                 extraction_exit_status NOT NULL DEFAULT 'succeeded',
          http_status                 int,
          backend_status              text,
          raw_blob_sha256             text,
          raw_blob_uri                text,
          raw_blob_byte_length        bigint,
          raw_blob_mime_type          text,
          normalized_blob_sha256      text,
          normalized_blob_uri         text,
          normalized_blob_byte_length bigint,
          normalized_blob_mime_type   text,
          parser_used                 text,
          quality_metrics             jsonb,
          failure_class               extraction_failure_class NOT NULL DEFAULT 'none',
          retry_parent_id             uuid REFERENCES extraction_attempts(id) ON DELETE SET NULL,
          disposition                 extraction_disposition NOT NULL DEFAULT 'unassessed',
          error_message               text,
          selection_reason            text,
          selected                    boolean NOT NULL DEFAULT false,
          created_at                  timestamptz NOT NULL DEFAULT now(),

          PRIMARY KEY (id),
          CONSTRAINT chk_extraction_attempts_attempt_number
            CHECK (attempt_number >= 1),
          CONSTRAINT chk_extraction_attempts_raw_blob_sha
            CHECK (
              raw_blob_sha256 IS NULL
              OR (
                length(raw_blob_sha256) = 64
                AND raw_blob_sha256 ~ '^[0-9a-f]{64}$'
              )
            ),
          CONSTRAINT chk_extraction_attempts_normalized_blob_sha
            CHECK (
              normalized_blob_sha256 IS NULL
              OR (
                length(normalized_blob_sha256) = 64
                AND normalized_blob_sha256 ~ '^[0-9a-f]{64}$'
              )
            ),
          CONSTRAINT chk_extraction_attempts_quality_metrics
            CHECK (quality_metrics IS NULL OR jsonb_typeof(quality_metrics) = 'object'),
          CONSTRAINT uk_extraction_attempts_candidate_attempt
            UNIQUE (candidate_id, attempt_number)
        );
        """
    )

    # ----------------------------------------------------------------
    # 6. Add indexes
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'extraction_attempts' AND indexname = 'idx_extraction_attempts_candidate'
          ) THEN
            CREATE INDEX idx_extraction_attempts_candidate
              ON extraction_attempts (candidate_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'extraction_attempts' AND indexname = 'idx_extraction_attempts_run'
          ) THEN
            CREATE INDEX idx_extraction_attempts_run
              ON extraction_attempts (run_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'extraction_attempts' AND indexname = 'idx_extraction_attempts_selected'
          ) THEN
            CREATE INDEX idx_extraction_attempts_selected
              ON extraction_attempts (candidate_id) WHERE selected = true;
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'extraction_attempts' AND indexname = 'idx_extraction_attempts_retry_parent'
          ) THEN
            CREATE INDEX idx_extraction_attempts_retry_parent
              ON extraction_attempts (retry_parent_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'extraction_attempts' AND indexname = 'idx_extraction_attempts_exit_status'
          ) THEN
            CREATE INDEX idx_extraction_attempts_exit_status
              ON extraction_attempts (exit_status);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'extraction_attempts' AND indexname = 'idx_extraction_attempts_disposition'
          ) THEN
            CREATE INDEX idx_extraction_attempts_disposition
              ON extraction_attempts (disposition);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'extraction_attempts' AND indexname = 'idx_extraction_attempts_method'
          ) THEN
            CREATE INDEX idx_extraction_attempts_method
              ON extraction_attempts (method);
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 7. Record migration
    # ----------------------------------------------------------------
    op.execute(
        "INSERT INTO schema_migrations(version) VALUES (21) ON CONFLICT DO NOTHING"
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v21 recovery boundary or apply a forward repair "
        "migration."
    )
