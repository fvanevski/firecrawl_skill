"""Persist immutable, versioned budget snapshots per research run.

The revision is additive. It does not rewrite corpus, snapshot, derivation,
index, job, lease, provenance, or existing workflow records.
"""

from alembic import op


revision = "0007_budget_snapshots"
down_revision = "0006_workflow_state"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE research_budget_snapshots(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id),
          research_spec_id uuid NOT NULL,
          spec_revision bigint NOT NULL CHECK(spec_revision > 0),
          run_revision bigint NOT NULL CHECK(run_revision >= 0),
          policy_version text NOT NULL CHECK(policy_version <> ''),
          policy_config_sha256 text NOT NULL
            CHECK(policy_config_sha256 ~ '^[0-9a-f]{64}$'),
          snapshot jsonb NOT NULL,
          content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
          idempotency_key text NOT NULL CHECK(idempotency_key <> ''),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(run_id,policy_version,run_revision),
          UNIQUE(run_id,idempotency_key),
          UNIQUE(id,run_id),
          FOREIGN KEY(research_spec_id,run_id)
            REFERENCES research_specs(id,run_id)
        );
        CREATE INDEX research_budget_snapshots_run_idx
          ON research_budget_snapshots(run_id,created_at,id);

        ALTER TABLE research_runs ADD COLUMN budget_snapshot_id uuid;
        ALTER TABLE research_runs ADD CONSTRAINT research_runs_budget_snapshot_fk
          FOREIGN KEY(budget_snapshot_id,id)
          REFERENCES research_budget_snapshots(id,run_id);

        INSERT INTO schema_migrations(version) VALUES (7) ON CONFLICT DO NOTHING;
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL from "
        "the pre-v7 recovery boundary or apply a forward repair migration"
    )
