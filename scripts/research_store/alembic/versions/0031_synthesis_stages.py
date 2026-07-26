"""Add synthesis_stages table for bounded autonomous-local synthesis.

This migration introduces the schema required by issue #63.

## Schema changes

### synthesis_stages

Tracks bounded synthesis stage state per research run. Each stage is
independently retryable and resumable.

* ``id`` — UUID PK, ``gen_random_uuid()``
* ``run_id`` — FK to ``research_runs(id)`` ON DELETE CASCADE
* ``stage_name`` — one of: ``outline``, ``binding``, ``draft``, ``citation_pass``
* ``stage_status`` — one of: ``pending``, ``running``, ``completed``, ``failed``, ``skipped``
* ``semantic_call_id`` — FK to ``semantic_calls(id)`` ON DELETE SET NULL
* ``semantic_artifact_id`` — FK to ``semantic_artifacts(id)`` ON DELETE SET NULL
* ``evidence_packet_revision`` — the EvidencePacket revision this stage operated against
* ``model_name`` — the model used for this stage (local endpoint only)
* ``prompt_version`` — the prompt template version
* ``schema_version`` — the output schema version
* ``artifact`` — JSONB blob containing the stage output artifact (when completed)
* ``error`` — text error message (when failed)
* ``attempts`` — integer attempt count
* ``created_at`` — timestamptz, defaults to ``now()``
* ``updated_at`` — timestamptz, defaults to ``now()``

Constraints:
* ``uk_synthesis_stages_run_stage`` — unique ``(run_id, stage_name)`` ensures
  each stage is recorded at most once per run (append via ``updated_at``).

## Design decisions

* The table is append-only in spirit: each ``run_id`` + ``stage_name`` pair
  has a single row. Updates modify ``stage_status``, ``artifact``, ``error``,
  and ``updated_at``. New rows are not inserted for retries.
* ``semantic_call_id`` and ``semantic_artifact_id`` link to the existing
  semantic provenance tables so that the full model-call chain is traceable.
* Only local endpoints are permitted. The ``ReportService`` enforces this
  at the Python level; the schema does not need a CHECK constraint because
  the application validates the model source before insertion.

## Compatibility

* Forward-only migration. No data is written to existing runs.
* The ``synthesis_stages`` table is created with ``IF NOT EXISTS`` guards
  so that repeated application is safe.
* No existing tables are modified.
* Phase 1–6 data is preserved.
"""

from alembic import op

revision = "0031_synthesis_stages"
down_revision = "0030_duplicate_groups"
branch_labels = None
depends_on = None

STAGE_NAMES = ("outline", "binding", "draft", "citation_pass")
STAGE_STATUSES = ("pending", "running", "completed", "failed", "skipped")


def upgrade():
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS synthesis_stages (
          id                          uuid NOT NULL DEFAULT gen_random_uuid(),
          run_id                      uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
          stage_name                  text NOT NULL CHECK (stage_name IN {tuple(STAGE_NAMES)}),
          stage_status                text NOT NULL DEFAULT 'pending'
                                        CHECK (stage_status IN {tuple(STAGE_STATUSES)}),
          semantic_call_id            uuid REFERENCES semantic_calls(id) ON DELETE SET NULL,
          semantic_artifact_id        uuid REFERENCES semantic_artifacts(id) ON DELETE SET NULL,
          evidence_packet_revision    bigint NOT NULL,
          model_name                  text NOT NULL,
          prompt_version              text NOT NULL,
          schema_version              bigint NOT NULL,
          artifact                    jsonb,
          error                       text,
          attempts                    bigint NOT NULL DEFAULT 1,
          created_at                  timestamptz NOT NULL DEFAULT now(),
          updated_at                  timestamptz NOT NULL DEFAULT now(),

          PRIMARY KEY (id),
          CONSTRAINT uk_synthesis_stages_run_stage
            UNIQUE (run_id, stage_name)
        );
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'synthesis_stages' AND indexname = 'idx_synthesis_stages_run'
          ) THEN
            CREATE INDEX idx_synthesis_stages_run
              ON synthesis_stages (run_id);
          END IF;
        END $$;
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'synthesis_stages' AND indexname = 'idx_synthesis_stages_status'
          ) THEN
            CREATE INDEX idx_synthesis_stages_status
              ON synthesis_stages (run_id, stage_status);
          END IF;
        END $$;
        """
    )

    op.execute(
        "INSERT INTO schema_migrations(version) VALUES (31) ON CONFLICT DO NOTHING"
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v31 recovery boundary or apply a forward repair "
        "migration."
    )
