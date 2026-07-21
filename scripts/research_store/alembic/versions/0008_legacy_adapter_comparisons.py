"""Add append-only legacy adapter comparison records.

The revision is additive. It does not rewrite workflow, corpus, snapshot,
derivation, index, job, lease, provenance, or compatibility-export records.
"""

from alembic import op


revision = "0008_legacy_adapter_comparisons"
down_revision = "0007_budget_snapshots"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE legacy_adapter_comparisons(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid REFERENCES research_runs(id),
          external_run_id text,
          external_invocation_id text,
          entry_point text NOT NULL CHECK(entry_point IN (
            'frun','fsearch_smart','fsearch','fscrape'
          )),
          adapter_mode text NOT NULL CHECK(adapter_mode IN ('shadow','authoritative')),
          legacy_decision jsonb NOT NULL,
          service_proposal jsonb NOT NULL,
          legacy_sha256 text NOT NULL CHECK(legacy_sha256 ~ '^[0-9a-f]{64}$'),
          proposal_sha256 text NOT NULL CHECK(proposal_sha256 ~ '^[0-9a-f]{64}$'),
          divergent boolean NOT NULL,
          divergence_reasons jsonb NOT NULL DEFAULT '[]',
          workflow_revision bigint CHECK(workflow_revision IS NULL OR workflow_revision >= 0),
          idempotency_key text NOT NULL CHECK(idempotency_key <> ''),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(idempotency_key),
          CHECK(run_id IS NOT NULL OR external_run_id IS NOT NULL
            OR external_invocation_id IS NOT NULL)
        );
        CREATE INDEX legacy_adapter_comparisons_run_idx
          ON legacy_adapter_comparisons(run_id,created_at,id);
        CREATE INDEX legacy_adapter_comparisons_external_run_idx
          ON legacy_adapter_comparisons(external_run_id,created_at,id);
        CREATE INDEX legacy_adapter_comparisons_divergence_idx
          ON legacy_adapter_comparisons(entry_point,created_at,id)
          WHERE divergent;

        CREATE TRIGGER legacy_adapter_comparisons_append_only
          BEFORE UPDATE OR DELETE ON legacy_adapter_comparisons
          FOR EACH ROW EXECUTE FUNCTION reject_workflow_ledger_mutation();

        INSERT INTO schema_migrations(version) VALUES (8) ON CONFLICT DO NOTHING;
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; retain adapter comparison "
        "history and apply a forward repair migration"
    )
