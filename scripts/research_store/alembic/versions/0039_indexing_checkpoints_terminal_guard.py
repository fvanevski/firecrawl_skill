"""Persist indexing checkpoints and require terminal-decision provenance.

The migration is additive. It records only checkpoints created after upgrade;
no historical membership or census is inferred. Existing terminal decisions
receive explicit ``legacy_unstructured`` markers rather than fabricated
provenance. The terminal-transition trigger is ``NOT`` a backfill: it enforces
that every new terminal transition is preceded by a decision in the same
transaction and under the same run-scoped idempotency key.
"""

from alembic import op

revision = "0039_indexing_checkpoints_terminal_guard"
down_revision = "0038_postgres_authority"
branch_labels = None
depends_on = None


def upgrade():
    op.get_bind().exec_driver_sql(
        r"""
        ALTER TABLE terminal_decisions
          ADD COLUMN reason_code text NOT NULL DEFAULT 'legacy_unstructured',
          ADD COLUMN state_census jsonb NOT NULL DEFAULT
            '{"schema_version":"terminal-state-census-v1","available":false,"reason":"legacy_unstructured"}'::jsonb;

        ALTER TABLE terminal_decisions
          ADD CONSTRAINT chk_terminal_decisions_reason_code
            CHECK (length(btrim(reason_code)) > 0),
          ADD CONSTRAINT chk_terminal_decisions_state_census_object
            CHECK (jsonb_typeof(state_census) = 'object');

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

        CREATE OR REPLACE FUNCTION _require_terminal_decision_for_transition()
        RETURNS trigger AS $$
        BEGIN
          IF NEW.next_state IN ('completed', 'partial', 'failed', 'cancelled')
             AND NOT EXISTS (
               SELECT 1
                 FROM terminal_decisions decision
                WHERE decision.run_id = NEW.run_id
                  AND decision.idempotency_key = NEW.idempotency_key
             ) THEN
            RAISE EXCEPTION
              'terminal transition requires terminal_decisions row for run %% and idempotency key %%',
              NEW.run_id, NEW.idempotency_key
              USING ERRCODE = '23514',
                    CONSTRAINT = 'terminal_transition_requires_decision';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER terminal_transition_requires_decision_trigger
          BEFORE INSERT ON research_run_transitions
          FOR EACH ROW EXECUTE FUNCTION _require_terminal_decision_for_transition();
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; apply a forward repair "
        "migration or restore PostgreSQL from the pre-v39 recovery boundary."
    )
