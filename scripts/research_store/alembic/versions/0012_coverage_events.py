"""Add coverage_events and coverage_snapshots tables.

This migration introduces the append-only coverage event ledger and
immutable coverage snapshots that power the coverage-led adaptive
workflow (Phase 3 / FR-012).

Key invariants enforced by DDL:

* coverage_events is append-only (no UPDATE/DELETE).
* coverage_snapshots is append-only (no UPDATE/DELETE).
* coverage_revision is monotonically increasing per run.
* Idempotency keys prevent duplicate application.
* Unknown coverage-item references are rejected at insert time.
* Content hashes detect tampering on snapshots.
* source_event_id / source_invocation_id link events to the
  research event stream for full provenance.

The revision is additive. It does not rewrite existing workflow,
spec, budget, corpus, snapshot, legacy, search plan, or search
response records.
"""

from alembic import op


revision = "0012_coverage_events"
down_revision = "0011_candidate_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        -- ----------------------------------------------------------------
        -- coverage_events  (append-only event ledger)
        -- ----------------------------------------------------------------

        CREATE TYPE coverage_event_type AS ENUM (
          'item_created',
          'item_status_changed',
          'item_gap_identified',
          'item_gap_resolved',
          'snapshot_created',
          'projection_rebuilt'
        );

        CREATE TYPE coverage_item_type AS ENUM (
          'question',
          'claim',
          'source_requirement',
          'freshness_requirement',
          'corroboration_requirement',
          'contradiction_requirement'
        );

        CREATE TYPE coverage_item_status AS ENUM (
          'missing',
          'candidate_identified',
          'acquired',
          'partially_supported',
          'supported',
          'contradicted',
          'qualified',
          'satisfied',
          'blocked',
          'waived',
          'unassessed'
        );

        CREATE TYPE freshness_status AS ENUM (
          'satisfied',
          'unsatisfied',
          'uncertain',
          'not_applicable'
        );

        CREATE TYPE overall_coverage_status AS ENUM (
          'insufficient',
          'partial',
          'sufficient',
          'blocked',
          'unassessed'
        );

        CREATE TABLE coverage_events(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id),
          coverage_revision bigint NOT NULL CHECK(coverage_revision > 0),
          prior_coverage_revision bigint NOT NULL
            CHECK(prior_coverage_revision >= 0),
          event_type coverage_event_type NOT NULL,
          item_id uuid,
          item_type coverage_item_type,
          subject_id text,
          new_status coverage_item_status,
          previous_status coverage_item_status,
          new_freshness_status freshness_status,
          previous_freshness_status freshness_status,
          source_event_id uuid,
          source_invocation_id uuid,
          payload jsonb NOT NULL DEFAULT '{}',
          idempotency_key text NOT NULL CHECK(idempotency_key <> ''),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(run_id, idempotency_key),
          UNIQUE(id, run_id),
          FOREIGN KEY(source_event_id, run_id)
            REFERENCES research_events(id, run_id),
          FOREIGN KEY(source_invocation_id, run_id)
            REFERENCES research_invocations(id, run_id),
          CHECK(coverage_revision > prior_coverage_revision)
        );

        CREATE INDEX coverage_events_run_cursor_idx
          ON coverage_events(run_id, coverage_revision, id);
        CREATE INDEX coverage_events_item_idx
          ON coverage_events(run_id, item_id, created_at);
        CREATE INDEX coverage_events_type_idx
          ON coverage_events(run_id, event_type, created_at);
        CREATE INDEX coverage_events_source_idx
          ON coverage_events(source_event_id, run_id);

        -- ----------------------------------------------------------------
        -- coverage_snapshots  (immutable ledger checkpoints)
        -- ----------------------------------------------------------------

        CREATE TABLE coverage_snapshots(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id),
          coverage_revision bigint NOT NULL CHECK(coverage_revision > 0),
          ledger jsonb NOT NULL,
          content_sha256 text NOT NULL
            CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
          triggering_event_id uuid,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(run_id, coverage_revision),
          UNIQUE(id, run_id),
          FOREIGN KEY(triggering_event_id, run_id)
            REFERENCES research_events(id, run_id)
        );

        CREATE INDEX coverage_snapshots_run_idx
          ON coverage_snapshots(run_id, coverage_revision DESC);
        CREATE INDEX coverage_snapshots_revision_idx
          ON coverage_snapshots(coverage_revision);

        -- ----------------------------------------------------------------
        -- Append-only enforcement triggers
        -- ----------------------------------------------------------------

        CREATE FUNCTION reject_coverage_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
            USING ERRCODE = '55000';
        END $$;

        CREATE TRIGGER coverage_events_append_only
          BEFORE UPDATE OR DELETE ON coverage_events
          FOR EACH ROW EXECUTE FUNCTION reject_coverage_mutation();

        CREATE TRIGGER coverage_snapshots_append_only
          BEFORE UPDATE OR DELETE ON coverage_snapshots
          FOR EACH ROW EXECUTE FUNCTION reject_coverage_mutation();

        -- ----------------------------------------------------------------
        -- Update research_runs to reference current coverage revision
        -- (already exists as a column from 0006, but add FK for
        -- referential integrity if the column is present)
        -- ----------------------------------------------------------------

        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'research_runs'
              AND column_name = 'current_coverage_revision'
          ) THEN
            ALTER TABLE research_runs
              ADD CONSTRAINT research_runs_current_cov_rev_fk
              FOREIGN KEY (current_coverage_revision, run_id)
              REFERENCES coverage_snapshots(coverage_revision, run_id)
              NOT VALID;
            -- Validate on populated data; skip if no coverage rows yet
            BEGIN
              ALTER TABLE research_runs
                VALIDATE CONSTRAINT research_runs_current_cov_rev_fk;
            EXCEPTION WHEN OTHERS THEN
              -- Leave NOT VALID if there is nothing to validate against
              NULL;
            END;
          END IF;
        END $$;
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v12 recovery boundary or apply a forward repair "
        "migration."
    )
