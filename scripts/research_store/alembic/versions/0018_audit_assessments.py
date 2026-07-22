"""Add audit_assessments and audit_stage_outputs tables.

This migration introduces the tables required by issue #33
(Staged semantic audit persistence) and PRD section FR-017.

## Schema changes

### audit_assessments

Top-level audit assessment records. Each row represents one complete or
partial audit invocation for a research run or invocation target.

* ``id`` — UUID PK, ``gen_random_uuid()``
* ``run_id`` — FK to ``research_runs(id)`` ON DELETE CASCADE
* ``target_type`` — ``'run'`` or ``'invocation'`` (which entity was audited)
* ``target_id`` — domain-level UUID of the audited entity
* ``target_hash`` — SHA-256 hex digest of the audit packet at assessment time
* ``evaluator_version`` — evaluator version string (e.g. ``catalog-v5.0``)
* ``prompt_template_version`` — prompt template version (e.g. ``staged-research-audit-v1``)
* ``policy_version`` — audit policy version applied
* ``stage_set`` — text array of stages that were requested (e.g. ``{rubric,acquisition,evidence,synthesis}``)
* ``status`` — constrained to ``audit_status`` enum
* ``provider`` — primary provider used (e.g. ``local``, ``openai``)
* ``model`` — model identifier used (nullable)
* ``prompt_hash`` — SHA-256 hex digest of the prompt sent to the model
* ``model_fingerprint`` — model fingerprint for provenance (nullable)
* ``elapsed_ms`` — total wall-clock time in milliseconds
* ``audit_packet_manifest`` — JSONB summary of the audit packet context
* ``created_at`` — timestamptz, defaults to ``now()``

Constraints:
* ``chk_audit_assessments_target_type`` — enum constraint.
* ``chk_audit_assessments_status`` — enum constraint.
* ``chk_audit_assessments_target_hash`` — non-empty hash.

Indexes:
* ``idx_audit_assessments_run`` — filter by ``run_id``
* ``idx_audit_assessments_target`` — filter by ``(target_type, target_id)``
* ``idx_audit_assessments_target_hash`` — filter by ``target_hash``
* ``idx_audit_assessments_status`` — filter by ``status``

### audit_stage_outputs

Individual stage results within an assessment. Append-only — no UPDATE/DELETE.
A stage failure does not erase successful stages.

* ``id`` — UUID PK, ``gen_random_uuid()``
* ``assessment_id`` — FK to ``audit_assessments(id)`` ON DELETE CASCADE
* ``stage`` — constrained to ``audit_stage`` enum
* ``sequence_number`` — integer, ordering within an assessment
* ``status`` — constrained to ``audit_stage_status`` enum
* ``output`` — JSONB stage output (nullable for failed stages)
* ``error`` — free-text error description (nullable)
* ``error_details`` — JSONB structured error details (nullable)
* ``call_count`` — number of model calls made for this stage
* ``used_fallback`` — boolean, whether a fallback was used
* ``created_at`` — timestamptz, defaults to ``now()``

Constraints:
* ``chk_audit_stage_outputs_stage`` — enum constraint.
* ``chk_audit_stage_outputs_status`` — enum constraint.
* ``chk_audit_stage_outputs_sequence_number`` — positive integer.
* ``uk_audit_stage_outputs_assessment_stage_seq`` — unique ``(assessment_id, stage, sequence_number)``
  allows multiple chunks of the same stage (e.g. acquisition chunks).

Indexes:
* ``idx_audit_stage_outputs_assessment`` — filter by ``assessment_id``
* ``idx_audit_stage_outputs_stage`` — filter by ``stage``
* ``idx_audit_stage_outputs_status`` — filter by ``status``

## Referential invariants

1. ``audit_assessments.run_id`` references ``research_runs(id)``.
2. ``audit_stage_outputs.assessment_id`` references ``audit_assessments(id)``.
3. ``target_id`` must be a valid UUID that exists in the referenced entity
   (validated at the service layer before insertion).

## Staleness semantics

The ``target_hash`` captures the SHA-256 of the audit packet at assessment
time. When the underlying evidence or report changes, the packet hash
changes, making prior assessments stale. The ``AuditService`` exposes
``detect_stale_assessments`` to identify assessments whose
``target_hash`` no longer matches the current packet hash.

Stale assessments are retained as historical records — they are never
overwritten or deleted. New assessments are appended as new rows.

## Deterministic reference validation

The ``AuditService`` validates that evidence references in stage outputs
refer to known IDs (claims, passages, snapshots) before accepting.
Invented evidence references cause the stage to be marked as failed with
referential-validation errors recorded in ``error_details``.

## Import/export behavior

* ``audit-export`` — serializes a complete assessment with all stage
  outputs as JSON, including target hash, provenance, and status.
* ``audit-query`` — returns assessments filtered by run, target, or status.

## Catalog v5 compatibility

Filesystem (Catalog v5 assessment files, scratch directories) is derived
only — never authoritative. Regenerating Catalog v5 assessment files from
PostgreSQL state is a Phase 5 deliverable (issue #35 — Catalog v5 exporter).
Until then, deleting Catalog v5 files does not affect audit availability.

## Partial-stage behavior

Stage failures are recorded individually in ``audit_stage_outputs`` with
``status = 'failed'`` and an error description. Successful stages remain
intact. The parent assessment ``status`` is ``partial`` when some stages
succeeded and some failed, ``completed`` when all stages succeeded, and
``failed`` when none succeeded.

## Forward-repair

If this migration is interrupted, re-run ``upgrade head`` from the
last successful revision. The migration is idempotent — tables and enums
use ``CREATE TABLE IF NOT EXISTS`` and ``CREATE TYPE ... NOT EXISTS`` guards.

## Downgrade

Migrations are forward-only. Restore PostgreSQL from the pre-v18
recovery boundary or apply a forward-repair migration.
"""

from alembic import op


revision = "0018_audit_assessments"
down_revision = "0017_claims_evidence"
branch_labels = None
depends_on = None


def upgrade():
    # ----------------------------------------------------------------
    # 1. Create the audit_status enum for audit_assessments
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_type WHERE typname = 'audit_status'
          ) THEN
            CREATE TYPE audit_status AS ENUM (
              'completed',
              'partial',
              'failed'
            );
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 2. Create the audit_stage enum
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_type WHERE typname = 'audit_stage'
          ) THEN
            CREATE TYPE audit_stage AS ENUM (
              'rubric',
              'acquisition',
              'evidence',
              'synthesis'
            );
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 3. Create the audit_stage_status enum
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_type WHERE typname = 'audit_stage_status'
          ) THEN
            CREATE TYPE audit_stage_status AS ENUM (
              'completed',
              'failed',
              'skipped'
            );
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 4. Create the target_type enum
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_type WHERE typname = 'audit_target_type'
          ) THEN
            CREATE TYPE audit_target_type AS ENUM (
              'run',
              'invocation'
            );
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 5. Create audit_assessments table
    # ----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_assessments (
          id                          uuid NOT NULL DEFAULT gen_random_uuid(),
          run_id                      uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
          target_type                 audit_target_type NOT NULL,
          target_id                   uuid NOT NULL,
          target_hash                 text NOT NULL,
          evaluator_version           text NOT NULL,
          prompt_template_version     text NOT NULL,
          policy_version              text NOT NULL,
          stage_set                   text[] NOT NULL,
          status                      audit_status NOT NULL,
          provider                    text,
          model                       text,
          prompt_hash                 text,
          model_fingerprint           text,
          elapsed_ms                  bigint NOT NULL DEFAULT 0,
          audit_packet_manifest       jsonb,
          created_at                  timestamptz NOT NULL DEFAULT now(),

          PRIMARY KEY (id),
          CONSTRAINT chk_audit_assessments_target_type
            CHECK (target_type IS NOT NULL),
          CONSTRAINT chk_audit_assessments_status
            CHECK (status IS NOT NULL),
          CONSTRAINT chk_audit_assessments_target_hash
            CHECK (length(trim(target_hash)) > 0),
          CONSTRAINT uk_audit_assessments_target
            UNIQUE (run_id, target_type, target_id, target_hash)
        );
        """
    )

    # ----------------------------------------------------------------
    # 6. Create audit_stage_outputs table
    # ----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_stage_outputs (
          id                          uuid NOT NULL DEFAULT gen_random_uuid(),
          assessment_id               uuid NOT NULL REFERENCES audit_assessments(id) ON DELETE CASCADE,
          stage                       audit_stage NOT NULL,
          sequence_number             bigint NOT NULL,
          status                      audit_stage_status NOT NULL,
          output                      jsonb,
          error                       text,
          error_details               jsonb,
          call_count                  bigint NOT NULL DEFAULT 0,
          used_fallback               boolean NOT NULL DEFAULT false,
          created_at                  timestamptz NOT NULL DEFAULT now(),

          PRIMARY KEY (id),
          CONSTRAINT chk_audit_stage_outputs_stage
            CHECK (stage IS NOT NULL),
          CONSTRAINT chk_audit_stage_outputs_status
            CHECK (status IS NOT NULL),
          CONSTRAINT chk_audit_stage_outputs_sequence_number
            CHECK (sequence_number > 0),
          CONSTRAINT uk_audit_stage_outputs_assessment_stage_seq
            UNIQUE (assessment_id, stage, sequence_number)
        );
        """
    )

    # ----------------------------------------------------------------
    # 7. Add indexes
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'audit_assessments' AND indexname = 'idx_audit_assessments_run'
          ) THEN
            CREATE INDEX idx_audit_assessments_run
              ON audit_assessments (run_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'audit_assessments' AND indexname = 'idx_audit_assessments_target'
          ) THEN
            CREATE INDEX idx_audit_assessments_target
              ON audit_assessments (target_type, target_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'audit_assessments' AND indexname = 'idx_audit_assessments_target_hash'
          ) THEN
            CREATE INDEX idx_audit_assessments_target_hash
              ON audit_assessments (target_hash);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'audit_assessments' AND indexname = 'idx_audit_assessments_status'
          ) THEN
            CREATE INDEX idx_audit_assessments_status
              ON audit_assessments (status);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'audit_stage_outputs' AND indexname = 'idx_audit_stage_outputs_assessment'
          ) THEN
            CREATE INDEX idx_audit_stage_outputs_assessment
              ON audit_stage_outputs (assessment_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'audit_stage_outputs' AND indexname = 'idx_audit_stage_outputs_stage'
          ) THEN
            CREATE INDEX idx_audit_stage_outputs_stage
              ON audit_stage_outputs (stage);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'audit_stage_outputs' AND indexname = 'idx_audit_stage_outputs_status'
          ) THEN
            CREATE INDEX idx_audit_stage_outputs_status
              ON audit_stage_outputs (status);
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 8. Record migration
    # ----------------------------------------------------------------
    op.execute(
        "INSERT INTO schema_migrations(version) VALUES (18) ON CONFLICT DO NOTHING"
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v18 recovery boundary or apply a forward repair "
        "migration."
    )