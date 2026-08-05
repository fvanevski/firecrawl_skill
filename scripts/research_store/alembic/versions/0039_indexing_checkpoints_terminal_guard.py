"""Persist indexing checkpoints and bind terminal decisions atomically.

The migration is additive and forward-only. It records only checkpoints created
after upgrade and never infers historical membership or census evidence.
Terminal decisions that already exist at the migration boundary receive explicit
``legacy_unstructured`` markers. New terminal decisions must instead carry
structured reason/census fields and commit in the same PostgreSQL transaction as
one semantically matching terminal lifecycle transition.
"""

from alembic import op

revision = "0039_index_checkpoint_guard"
down_revision = "0038_postgres_authority"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        r"""
        ALTER TABLE terminal_decisions
          ADD COLUMN reason_code text,
          ADD COLUMN state_census jsonb,
          ADD COLUMN decision_transaction_id xid8;

        UPDATE terminal_decisions
           SET reason_code='legacy_unstructured',
               state_census=
                 '{"schema_version":"terminal-state-census-v1","available":false,"reason":"legacy_unstructured"}'::jsonb
         WHERE reason_code IS NULL OR state_census IS NULL;

        ALTER TABLE terminal_decisions
          ALTER COLUMN reason_code SET NOT NULL,
          ALTER COLUMN state_census SET NOT NULL,
          ALTER COLUMN decision_transaction_id SET DEFAULT pg_current_xact_id(),
          ADD CONSTRAINT chk_terminal_decisions_reason_code
            CHECK (length(btrim(reason_code)) > 0),
          ADD CONSTRAINT chk_terminal_decisions_state_census_object
            CHECK (jsonb_typeof(state_census) = 'object'),
          ADD CONSTRAINT chk_terminal_decisions_provenance_boundary
            CHECK (
              (
                reason_code = 'legacy_unstructured'
                AND decision_transaction_id IS NULL
                AND state_census->>'reason' = 'legacy_unstructured'
              )
              OR
              (
                reason_code <> 'legacy_unstructured'
                AND decision_transaction_id IS NOT NULL
                AND COALESCE(state_census->>'reason','') <> 'legacy_unstructured'
              )
            );

        ALTER TABLE research_run_transitions
          ADD COLUMN transition_transaction_id xid8;
        ALTER TABLE research_run_transitions
          ALTER COLUMN transition_transaction_id SET DEFAULT pg_current_xact_id();

        CREATE TABLE indexing_checkpoints (
          id                              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id                          uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
          lifecycle_revision              bigint NOT NULL CHECK (lifecycle_revision >= 0),
          fingerprint                     text NOT NULL CHECK (length(btrim(fingerprint)) > 0),
          entity_ids                      uuid[] NOT NULL DEFAULT '{}',
          expected_membership_sha256      text NOT NULL,
          expected_count                  bigint NOT NULL CHECK (expected_count >= 0),
          complete_count                  bigint NOT NULL DEFAULT 0 CHECK (complete_count >= 0),
          manifest_count                  bigint NOT NULL DEFAULT 0 CHECK (manifest_count >= 0),
          census_counts                   jsonb NOT NULL DEFAULT '{}'::jsonb,
          latest_relevant_worker_heartbeat jsonb,
          lease_expiration_bounds         jsonb,
          retry_available_at_bounds       jsonb,
          deadline_at                     timestamptz,
          status                          text NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'completed', 'invalidated')),
          invalidation_reason             text,
          idempotency_key                 text NOT NULL,
          created_at                      timestamptz NOT NULL DEFAULT now(),
          updated_at                      timestamptz NOT NULL DEFAULT now(),
          last_observed_at                timestamptz,
          completed_at                    timestamptz,
          invalidated_at                  timestamptz,
          CONSTRAINT uk_indexing_checkpoints_idempotency
            UNIQUE (run_id, idempotency_key),
          CONSTRAINT chk_indexing_checkpoint_hash
            CHECK (expected_membership_sha256 ~ '^[0-9a-f]{64}$'),
          CONSTRAINT chk_indexing_checkpoint_membership_count
            CHECK (cardinality(entity_ids) = expected_count),
          CONSTRAINT chk_indexing_checkpoint_census_object
            CHECK (jsonb_typeof(census_counts) = 'object')
        );

        CREATE TABLE indexing_checkpoint_observations (
          id                              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          checkpoint_id                   uuid NOT NULL
            REFERENCES indexing_checkpoints(id) ON DELETE CASCADE,
          observed_at                     timestamptz NOT NULL DEFAULT now(),
          expected_count                  bigint NOT NULL CHECK (expected_count >= 0),
          complete_count                  bigint NOT NULL CHECK (complete_count >= 0),
          manifest_count                  bigint NOT NULL CHECK (manifest_count >= 0),
          census_counts                   jsonb NOT NULL,
          latest_relevant_worker_heartbeat jsonb,
          lease_expiration_bounds         jsonb,
          retry_available_at_bounds       jsonb,
          CONSTRAINT chk_indexing_checkpoint_observation_census
            CHECK (jsonb_typeof(census_counts) = 'object')
        );

        CREATE INDEX indexing_checkpoint_observations_cursor_idx
          ON indexing_checkpoint_observations(checkpoint_id, observed_at, id);

        CREATE OR REPLACE FUNCTION _indexing_checkpoint_observations_append_only()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'indexing_checkpoint_observations is append-only';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER indexing_checkpoint_observations_append_only_trigger
          BEFORE UPDATE OR DELETE ON indexing_checkpoint_observations
          FOR EACH ROW EXECUTE FUNCTION _indexing_checkpoint_observations_append_only();

        CREATE UNIQUE INDEX uq_indexing_checkpoints_active_run
          ON indexing_checkpoints(run_id)
          WHERE status = 'active';
        CREATE INDEX indexing_checkpoints_run_cursor_idx
          ON indexing_checkpoints(run_id, created_at, id);
        CREATE INDEX indexing_checkpoints_membership_idx
          ON indexing_checkpoints(run_id, expected_membership_sha256);

        CREATE OR REPLACE FUNCTION _terminal_decision_target_state(
          decision_outcome terminal_decision_outcome
        ) RETURNS text AS $$
          SELECT CASE decision_outcome
            WHEN 'sufficient' THEN 'completed'
            WHEN 'partial' THEN 'partial'
            WHEN 'blocked' THEN 'partial'
            WHEN 'failed' THEN 'failed'
            WHEN 'cancelled' THEN 'cancelled'
          END
        $$ LANGUAGE sql IMMUTABLE STRICT;

        CREATE OR REPLACE FUNCTION _require_terminal_decision_for_transition()
        RETURNS trigger AS $$
        DECLARE
          decision terminal_decisions%%ROWTYPE;
        BEGIN
          IF NEW.next_state NOT IN ('completed', 'partial', 'failed', 'cancelled') THEN
            RETURN NEW;
          END IF;

          SELECT * INTO decision
            FROM terminal_decisions
           WHERE run_id = NEW.run_id
             AND idempotency_key = NEW.idempotency_key;

          IF NOT FOUND THEN
            RAISE EXCEPTION
              'terminal transition requires terminal_decisions row for run %% and idempotency key %%',
              NEW.run_id, NEW.idempotency_key
              USING ERRCODE = '23514',
                    CONSTRAINT = 'terminal_transition_requires_decision';
          END IF;

          IF decision.reason_code = 'legacy_unstructured'
             OR decision.decision_transaction_id IS NULL
             OR NEW.transition_transaction_id IS NULL
             OR decision.decision_transaction_id <> NEW.transition_transaction_id
             OR decision.decision_transaction_id <> pg_current_xact_id()
          THEN
            RAISE EXCEPTION
              'terminal decision and transition must be created in the same transaction'
              USING ERRCODE = '23514',
                    CONSTRAINT = 'terminal_decision_transition_same_transaction';
          END IF;

          IF decision.run_revision + 1 <> NEW.lifecycle_revision
             OR _terminal_decision_target_state(decision.outcome) <> NEW.next_state
          THEN
            RAISE EXCEPTION
              'terminal decision does not authorize transition to %% at lifecycle revision %%',
              NEW.next_state, NEW.lifecycle_revision
              USING ERRCODE = '23514',
                    CONSTRAINT = 'terminal_decision_transition_semantic_match';
          END IF;

          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER terminal_transition_requires_decision_trigger
          BEFORE INSERT ON research_run_transitions
          FOR EACH ROW EXECUTE FUNCTION _require_terminal_decision_for_transition();

        CREATE OR REPLACE FUNCTION _require_transition_for_terminal_decision()
        RETURNS trigger AS $$
        DECLARE
          transition research_run_transitions%%ROWTYPE;
        BEGIN
          IF NEW.reason_code = 'legacy_unstructured'
             OR NEW.decision_transaction_id IS NULL
             OR NEW.decision_transaction_id <> pg_current_xact_id()
          THEN
            RAISE EXCEPTION
              'new terminal decisions require structured same-transaction provenance'
              USING ERRCODE = '23514',
                    CONSTRAINT = 'new_terminal_decision_requires_structured_provenance';
          END IF;

          SELECT * INTO transition
            FROM research_run_transitions
           WHERE run_id = NEW.run_id
             AND idempotency_key = NEW.idempotency_key;

          IF NOT FOUND THEN
            RAISE EXCEPTION
              'terminal decision requires a matching terminal transition in the same transaction'
              USING ERRCODE = '23514',
                    CONSTRAINT = 'terminal_decision_requires_transition';
          END IF;

          IF transition.transition_transaction_id IS NULL
             OR transition.transition_transaction_id <> NEW.decision_transaction_id
             OR transition.transition_transaction_id <> pg_current_xact_id()
             OR transition.lifecycle_revision <> NEW.run_revision + 1
             OR _terminal_decision_target_state(NEW.outcome) <> transition.next_state
          THEN
            RAISE EXCEPTION
              'terminal decision and transition are not one atomic semantic command'
              USING ERRCODE = '23514',
                    CONSTRAINT = 'terminal_decision_transition_atomic_match';
          END IF;

          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER terminal_decision_requires_transition_trigger
          AFTER INSERT ON terminal_decisions
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION _require_transition_for_terminal_decision();
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; apply a forward repair "
        "migration or restore PostgreSQL from the pre-v39 recovery boundary."
    )
