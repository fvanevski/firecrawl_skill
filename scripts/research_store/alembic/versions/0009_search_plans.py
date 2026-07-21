"""Add versioned search plans and search plan queries tables.

The revision is additive. It does not rewrite workflow, spec, budget,
corpus, snapshot, or legacy records.
"""

from alembic import op


revision = "0009_search_plans"
down_revision = "0008_legacy_adapter_comparisons"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE search_plans(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id),
          research_spec_id uuid NOT NULL,
          revision bigint NOT NULL CHECK(revision > 0),
          schema_name text NOT NULL DEFAULT 'search-plan-v1',
          schema_version integer NOT NULL DEFAULT 1,
          status text NOT NULL DEFAULT 'active'
            CHECK(status IN ('active','superseded','cancelled')),
          payload jsonb NOT NULL,
          content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
          idempotency_key text NOT NULL CHECK(idempotency_key <> ''),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(run_id,revision),
          UNIQUE(run_id,idempotency_key),
          UNIQUE(id,run_id),
          FOREIGN KEY(research_spec_id,run_id)
            REFERENCES research_specs(id,run_id)
        );
        CREATE INDEX search_plans_run_idx
          ON search_plans(run_id,created_at,id);

        CREATE TABLE search_plan_queries(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          plan_id uuid NOT NULL,
          run_id uuid NOT NULL REFERENCES research_runs(id),
          query_index integer NOT NULL CHECK(query_index >= 0),
          query_text text NOT NULL CHECK(query_text <> ''),
          facet text NOT NULL CHECK(facet <> ''),
          target_question_ids jsonb NOT NULL DEFAULT '[]',
          target_claim_ids jsonb NOT NULL DEFAULT '[]',
          intended_source_classes jsonb NOT NULL DEFAULT '[]',
          expected_organizations jsonb NOT NULL DEFAULT '[]',
          freshness_requirement jsonb NOT NULL DEFAULT '{}',
          expected_contribution text NOT NULL CHECK(expected_contribution <> ''),
          domain_restrictions jsonb NOT NULL DEFAULT '[]',
          negative_terms jsonb NOT NULL DEFAULT '[]',
          priority integer NOT NULL DEFAULT 1 CHECK(priority >= 0),
          status text NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','executed','failed','cancelled')),
          payload jsonb NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(plan_id,query_index),
          UNIQUE(plan_id,id),
          UNIQUE(id,run_id),
          FOREIGN KEY(plan_id,run_id)
            REFERENCES search_plans(id,run_id)
        );
        CREATE INDEX search_plan_queries_plan_idx
          ON search_plan_queries(plan_id,query_index);
        CREATE INDEX search_plan_queries_run_idx
          ON search_plan_queries(run_id,created_at,id);

        ALTER TABLE research_runs ADD COLUMN search_plan_id uuid;
        ALTER TABLE research_runs ADD CONSTRAINT research_runs_search_plan_fk
          FOREIGN KEY(search_plan_id,id)
          REFERENCES search_plans(id,run_id);
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL from "
        "the pre-v9 recovery boundary or apply a forward repair migration"
    )
