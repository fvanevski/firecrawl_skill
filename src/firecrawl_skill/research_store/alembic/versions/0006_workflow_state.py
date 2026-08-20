"""Add the authoritative workflow-state foundation.

This revision establishes the PostgreSQL workflow authority on a clean schema.
PostgreSQL runs Alembic DDL transactionally, so an interrupted migration can be
retried with ``upgrade head``.
"""

from alembic import op

revision = "0006_workflow_state"
down_revision = "0004_manifest_definition_key"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        ALTER TABLE research_runs
          ADD COLUMN state text NOT NULL DEFAULT 'created',
          ADD COLUMN lifecycle_revision bigint NOT NULL DEFAULT 0,
          ADD COLUMN reopened_from_revision bigint,
          ADD COLUMN execution_mode text NOT NULL DEFAULT 'agent_led',
          ADD COLUMN budget_policy_version text,
          ADD COLUMN current_coverage_revision bigint NOT NULL DEFAULT 0,
          ADD COLUMN declared_outcome text,
          ADD COLUMN metadata jsonb NOT NULL DEFAULT '{}',
          ADD CONSTRAINT research_runs_state_check CHECK(state IN (
            'created','planning','corpus_review','acquiring','extracting',
            'indexing','coverage_review','retrieving','synthesizing',
            'validating','completed','partial','failed','cancelled'
          )),
          ADD CONSTRAINT research_runs_execution_mode_check CHECK(execution_mode IN (
            'agent_led','autonomous_local','deterministic_debug'
          )),
          ADD CONSTRAINT research_runs_revision_check CHECK(
            lifecycle_revision >= 0
            AND current_coverage_revision >= 0
            AND (reopened_from_revision IS NULL
              OR reopened_from_revision < lifecycle_revision)
          );
        CREATE INDEX research_runs_state_idx ON research_runs(state, started_at);

        CREATE TABLE research_invocations(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id),
          parent_invocation_id uuid,
          external_invocation_id text,
          operation text NOT NULL,
          status text NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','running','complete','partial','failed','cancelled')),
          lifecycle_revision bigint NOT NULL,
          idempotency_key text NOT NULL CHECK(idempotency_key <> ''),
          input jsonb NOT NULL DEFAULT '{}',
          output jsonb,
          error text,
          metadata jsonb NOT NULL DEFAULT '{}',
          started_at timestamptz,
          completed_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(run_id,idempotency_key),
          UNIQUE(external_invocation_id),
          UNIQUE(id,run_id),
          CHECK(lifecycle_revision >= 0),
          CHECK(parent_invocation_id IS NULL OR parent_invocation_id <> id),
          FOREIGN KEY(parent_invocation_id,run_id)
            REFERENCES research_invocations(id,run_id)
        );
        CREATE INDEX research_invocations_run_idx
          ON research_invocations(run_id,created_at,id);
        CREATE INDEX research_invocations_parent_idx
          ON research_invocations(parent_invocation_id);

        CREATE TABLE research_events(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id),
          invocation_id uuid,
          event_type text NOT NULL,
          actor_type text NOT NULL,
          actor_identifier text,
          payload jsonb NOT NULL DEFAULT '{}',
          run_revision bigint NOT NULL CHECK(run_revision >= 0),
          idempotency_key text NOT NULL CHECK(idempotency_key <> ''),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(run_id,idempotency_key),
          UNIQUE(id,run_id),
          FOREIGN KEY(invocation_id,run_id)
            REFERENCES research_invocations(id,run_id)
        );
        CREATE INDEX research_events_run_cursor_idx
          ON research_events(run_id,created_at,id);
        CREATE INDEX research_events_invocation_idx
          ON research_events(invocation_id,created_at,id);

        CREATE TABLE research_specs(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id),
          spec_revision bigint NOT NULL CHECK(spec_revision > 0),
          schema_name text NOT NULL,
          schema_version integer NOT NULL CHECK(schema_version > 0),
          payload jsonb NOT NULL,
          content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
          validation_status text NOT NULL
            CHECK(validation_status IN ('valid','invalid')),
          validation_errors jsonb NOT NULL DEFAULT '[]',
          idempotency_key text NOT NULL CHECK(idempotency_key <> ''),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(run_id,spec_revision),
          UNIQUE(run_id,idempotency_key),
          UNIQUE(id,run_id)
        );
        CREATE INDEX research_specs_run_idx
          ON research_specs(run_id,created_at,id);
        ALTER TABLE research_runs ADD COLUMN research_spec_id uuid;
        ALTER TABLE research_runs ADD CONSTRAINT research_runs_spec_fk
          FOREIGN KEY(research_spec_id,id) REFERENCES research_specs(id,run_id);

        CREATE TABLE semantic_calls(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id),
          invocation_id uuid,
          stage text NOT NULL,
          provider text NOT NULL,
          model text NOT NULL,
          model_revision text NOT NULL DEFAULT '',
          prompt_version text NOT NULL,
          input_sha256 text NOT NULL CHECK(input_sha256 ~ '^[0-9a-f]{64}$'),
          request jsonb NOT NULL DEFAULT '{}',
          response_metadata jsonb NOT NULL DEFAULT '{}',
          status text NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','running','complete','failed','cancelled')),
          error text,
          idempotency_key text NOT NULL CHECK(idempotency_key <> ''),
          started_at timestamptz,
          completed_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(run_id,idempotency_key),
          UNIQUE(id,run_id),
          FOREIGN KEY(invocation_id,run_id)
            REFERENCES research_invocations(id,run_id)
        );
        CREATE INDEX semantic_calls_run_idx
          ON semantic_calls(run_id,created_at,id);
        CREATE INDEX semantic_calls_invocation_idx
          ON semantic_calls(invocation_id,created_at,id);

        CREATE TABLE semantic_artifacts(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id),
          semantic_call_id uuid NOT NULL,
          artifact_type text NOT NULL,
          schema_name text NOT NULL,
          schema_version integer NOT NULL CHECK(schema_version > 0),
          payload jsonb NOT NULL,
          content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
          validation_status text NOT NULL
            CHECK(validation_status IN ('valid','invalid')),
          validation_errors jsonb NOT NULL DEFAULT '[]',
          idempotency_key text NOT NULL CHECK(idempotency_key <> ''),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(semantic_call_id,idempotency_key),
          UNIQUE(id,run_id),
          FOREIGN KEY(semantic_call_id,run_id)
            REFERENCES semantic_calls(id,run_id)
        );
        CREATE INDEX semantic_artifacts_call_idx
          ON semantic_artifacts(semantic_call_id,created_at,id);

        CREATE TABLE research_run_transitions(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id),
          lifecycle_revision bigint NOT NULL CHECK(lifecycle_revision > 0),
          prior_state text NOT NULL,
          next_state text NOT NULL,
          triggering_event_id uuid,
          actor_type text NOT NULL,
          actor_identifier text,
          policy_version text NOT NULL,
          semantic_proposal_id uuid,
          validation_result jsonb NOT NULL DEFAULT '{}',
          idempotency_key text NOT NULL CHECK(idempotency_key <> ''),
          error text,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(run_id,lifecycle_revision),
          UNIQUE(run_id,idempotency_key),
          CHECK(prior_state <> next_state),
          CHECK(prior_state IN (
            'created','planning','corpus_review','acquiring','extracting',
            'indexing','coverage_review','retrieving','synthesizing',
            'validating','completed','partial','failed','cancelled'
          )),
          CHECK(next_state IN (
            'created','planning','corpus_review','acquiring','extracting',
            'indexing','coverage_review','retrieving','synthesizing',
            'validating','completed','partial','failed','cancelled'
          )),
          FOREIGN KEY(triggering_event_id,run_id)
            REFERENCES research_events(id,run_id),
          FOREIGN KEY(semantic_proposal_id,run_id)
            REFERENCES semantic_artifacts(id,run_id)
        );
        CREATE INDEX research_run_transitions_run_idx
          ON research_run_transitions(run_id,lifecycle_revision);

        CREATE FUNCTION reject_workflow_ledger_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
            USING ERRCODE = '55000';
        END $$;
        CREATE TRIGGER research_run_transitions_append_only
          BEFORE UPDATE OR DELETE ON research_run_transitions
          FOR EACH ROW EXECUTE FUNCTION reject_workflow_ledger_mutation();
        CREATE TRIGGER research_events_append_only
          BEFORE UPDATE OR DELETE ON research_events
          FOR EACH ROW EXECUTE FUNCTION reject_workflow_ledger_mutation();
        """
    )


def downgrade():
    raise RuntimeError(
        "Research corpus migrations are forward-only; restore PostgreSQL from backup"
    )
