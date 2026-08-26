"""Add durable operator actions and explicit scope-fork lineage."""

from alembic import op

revision = "0045_operator_actions"
down_revision = "0044_terminal_provenance_guard"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE operator_actions (
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          external_action_id text NOT NULL UNIQUE,
          run_id uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
          lifecycle_revision bigint NOT NULL CHECK (lifecycle_revision >= 0),
          action_kind text NOT NULL CHECK (
            action_kind IN (
              'candidate_budget_authorization',
              'curation_selection_required',
              'material_scope_change_required',
              'manual_environment_resolution'
            )
          ),
          status text NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','resolved','superseded')),
          policy_version text NOT NULL,
          authority_fingerprint text NOT NULL,
          creation_payload jsonb NOT NULL,
          creation_sha256 text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          resolution_id uuid,
          resolution_actor text,
          resolution_reason text,
          resolution_payload jsonb,
          resolution_sha256 text,
          resolved_at timestamptz,
          CONSTRAINT operator_action_public_id CHECK (
            external_action_id ~ '^oa_[0-9a-f]{32}$'
          ),
          CONSTRAINT operator_action_authority_fingerprint CHECK (
            authority_fingerprint ~ '^[0-9a-f]{64}$'
          ),
          CONSTRAINT operator_action_creation_sha CHECK (
            creation_sha256 ~ '^[0-9a-f]{64}$'
          ),
          CONSTRAINT operator_action_resolution_sha CHECK (
            resolution_sha256 IS NULL OR resolution_sha256 ~ '^[0-9a-f]{64}$'
          ),
          CONSTRAINT operator_action_resolution_shape CHECK (
            (
              status='pending'
              AND resolution_id IS NULL
              AND resolution_actor IS NULL
              AND resolution_reason IS NULL
              AND resolution_payload IS NULL
              AND resolution_sha256 IS NULL
              AND resolved_at IS NULL
            )
            OR
            (
              status IN ('resolved','superseded')
              AND resolution_id IS NOT NULL
              AND resolution_actor IS NOT NULL
              AND length(btrim(resolution_actor)) > 0
              AND resolution_reason IS NOT NULL
              AND length(btrim(resolution_reason)) > 0
              AND resolution_payload IS NOT NULL
              AND resolution_sha256 IS NOT NULL
              AND resolved_at IS NOT NULL
            )
          ),
          UNIQUE(run_id,lifecycle_revision,action_kind,authority_fingerprint)
        );

        CREATE UNIQUE INDEX operator_actions_one_pending_per_run
          ON operator_actions(run_id) WHERE status='pending';
        CREATE INDEX operator_actions_run_created
          ON operator_actions(run_id,created_at,id);

        CREATE TABLE research_run_lineage (
          child_run_id uuid PRIMARY KEY REFERENCES research_runs(id) ON DELETE CASCADE,
          parent_run_id uuid NOT NULL REFERENCES research_runs(id) ON DELETE RESTRICT,
          operator_action_id uuid NOT NULL UNIQUE
            REFERENCES operator_actions(id) ON DELETE RESTRICT,
          parent_spec_id uuid REFERENCES research_specs(id) ON DELETE RESTRICT,
          parent_spec_revision bigint,
          reason text NOT NULL CHECK (length(btrim(reason)) > 0),
          child_objective text NOT NULL CHECK (length(btrim(child_objective)) > 0),
          created_at timestamptz NOT NULL DEFAULT now(),
          CHECK (child_run_id <> parent_run_id),
          CHECK (
            (parent_spec_id IS NULL AND parent_spec_revision IS NULL)
            OR
            (parent_spec_id IS NOT NULL AND parent_spec_revision IS NOT NULL)
          )
        );

        CREATE FUNCTION operator_action_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
          IF TG_OP='UPDATE' THEN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.external_action_id IS DISTINCT FROM OLD.external_action_id
               OR NEW.run_id IS DISTINCT FROM OLD.run_id
               OR NEW.lifecycle_revision IS DISTINCT FROM OLD.lifecycle_revision
               OR NEW.action_kind IS DISTINCT FROM OLD.action_kind
               OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
               OR NEW.authority_fingerprint IS DISTINCT FROM OLD.authority_fingerprint
               OR NEW.creation_payload IS DISTINCT FROM OLD.creation_payload
               OR NEW.creation_sha256 IS DISTINCT FROM OLD.creation_sha256
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
              RAISE EXCEPTION 'operator action creation authority is immutable'
                USING ERRCODE='55000';
            END IF;
            IF OLD.status <> 'pending' THEN
              RAISE EXCEPTION 'terminal operator action is immutable'
                USING ERRCODE='55000';
            END IF;
            IF NEW.status NOT IN ('resolved','superseded') THEN
              RAISE EXCEPTION 'operator action may only leave pending once'
                USING ERRCODE='55000';
            END IF;
          ELSIF TG_OP='DELETE' THEN
            IF NOT EXISTS (
              SELECT 1 FROM research_runs WHERE id=OLD.run_id
            ) THEN
              RETURN OLD;
            END IF;
            RAISE EXCEPTION 'operator actions are durable and cannot be deleted'
              USING ERRCODE='55000';
          END IF;
          IF TG_OP='DELETE' THEN
            RETURN OLD;
          END IF;
          RETURN NEW;
        END;
        $function$;

        CREATE TRIGGER operator_action_guard_trigger
        BEFORE UPDATE OR DELETE ON operator_actions
        FOR EACH ROW EXECUTE FUNCTION operator_action_guard();

        CREATE FUNCTION research_run_lineage_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
          IF TG_OP='DELETE' AND NOT EXISTS (
            SELECT 1 FROM research_runs WHERE id=OLD.child_run_id
          ) THEN
            RETURN OLD;
          END IF;
          RAISE EXCEPTION 'research run lineage is append-only'
            USING ERRCODE='55000';
        END;
        $function$;

        CREATE TRIGGER research_run_lineage_guard_trigger
        BEFORE UPDATE OR DELETE ON research_run_lineage
        FOR EACH ROW EXECUTE FUNCTION research_run_lineage_guard();
        """
    )


def downgrade():
    op.execute(
        """
        DROP TRIGGER IF EXISTS research_run_lineage_guard_trigger
          ON research_run_lineage;
        DROP FUNCTION IF EXISTS research_run_lineage_guard();
        DROP TABLE IF EXISTS research_run_lineage;

        DROP TRIGGER IF EXISTS operator_action_guard_trigger ON operator_actions;
        DROP FUNCTION IF EXISTS operator_action_guard();
        DROP TABLE IF EXISTS operator_actions;
        """
    )
