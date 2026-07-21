"""Add immutable raw search responses table.

The revision is additive. It does not rewrite workflow, spec, budget,
corpus, snapshot, legacy, or search plan records.
"""

from alembic import op


revision = "0010_search_responses"
down_revision = "0009_search_plans"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE search_responses(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id),
          plan_id uuid,
          plan_query_id uuid,
          query_text text NOT NULL CHECK(query_text <> ''),
          backend text NOT NULL CHECK(backend <> ''),
          provider_request_id text,
          status text NOT NULL
            CHECK(status IN ('succeeded','empty','provider_error','parse_error')),
          http_status integer,
          parser_version text NOT NULL CHECK(parser_version <> ''),
          raw_blob_sha256 text NOT NULL CHECK(raw_blob_sha256 ~ '^[0-9a-f]{64}$'),
          raw_blob_bytes bigint NOT NULL CHECK(raw_blob_bytes >= 0),
          mime_type text NOT NULL DEFAULT 'application/json',
          content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
          result_count integer NOT NULL DEFAULT 0 CHECK(result_count >= 0),
          error_message text,
          transport_metadata jsonb NOT NULL DEFAULT '{}',
          payload_summary jsonb NOT NULL DEFAULT '{}',
          idempotency_key text NOT NULL CHECK(idempotency_key <> ''),
          requested_at timestamptz NOT NULL DEFAULT now(),
          responded_at timestamptz NOT NULL DEFAULT now(),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(run_id, idempotency_key),
          UNIQUE(id, run_id),
          FOREIGN KEY(plan_id, run_id)
            REFERENCES search_plans(id, run_id),
          FOREIGN KEY(plan_query_id, run_id)
            REFERENCES search_plan_queries(id, run_id)
        );

        CREATE INDEX search_responses_run_idx
          ON search_responses(run_id, created_at, id);
        CREATE INDEX search_responses_plan_query_idx
          ON search_responses(plan_query_id);
        CREATE INDEX search_responses_blob_idx
          ON search_responses(raw_blob_sha256);
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL from "
        "the pre-v10 recovery boundary or apply a forward repair migration"
    )
