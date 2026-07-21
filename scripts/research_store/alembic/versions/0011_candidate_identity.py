"""Add search candidates and candidate occurrences tables.

The revision is additive. It does not rewrite workflow, spec, budget,
corpus, snapshot, legacy, search plan, or search response records.
"""

from alembic import op


revision = "0011_candidate_identity"
down_revision = "0010_search_responses"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE search_candidates(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id),
          canonical_url text NOT NULL CHECK(canonical_url <> ''),
          canonical_url_sha256 text NOT NULL CHECK(canonical_url_sha256 ~ '^[0-9a-f]{64}$'),
          original_url text NOT NULL CHECK(original_url <> ''),
          title text,
          snippet text,
          domain text NOT NULL CHECK(domain <> ''),
          backend text NOT NULL CHECK(backend <> ''),
          published_at timestamptz,
          date_signals jsonb NOT NULL DEFAULT '{}',
          backend_metadata jsonb NOT NULL DEFAULT '{}',
          recurrence_count integer NOT NULL DEFAULT 1 CHECK(recurrence_count >= 1),
          duplicate_group_id uuid,
          first_seen_at timestamptz NOT NULL DEFAULT now(),
          last_seen_at timestamptz NOT NULL DEFAULT now(),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(run_id, canonical_url_sha256),
          UNIQUE(id, run_id)
        );

        CREATE INDEX search_candidates_run_idx
          ON search_candidates(run_id, created_at, id);
        CREATE INDEX search_candidates_canonical_idx
          ON search_candidates(canonical_url_sha256);
        CREATE INDEX search_candidates_domain_idx
          ON search_candidates(domain);
        CREATE INDEX search_candidates_group_idx
          ON search_candidates(duplicate_group_id);

        CREATE TABLE candidate_occurrences(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          candidate_id uuid NOT NULL,
          run_id uuid NOT NULL REFERENCES research_runs(id),
          search_response_id uuid NOT NULL,
          plan_id uuid,
          plan_query_id uuid,
          rank integer NOT NULL CHECK(rank >= 1),
          query_text text NOT NULL CHECK(query_text <> ''),
          original_url text NOT NULL CHECK(original_url <> ''),
          title text,
          snippet text,
          raw_item jsonb NOT NULL DEFAULT '{}',
          discovered_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(search_response_id, rank),
          FOREIGN KEY(candidate_id, run_id)
            REFERENCES search_candidates(id, run_id),
          FOREIGN KEY(search_response_id, run_id)
            REFERENCES search_responses(id, run_id),
          FOREIGN KEY(plan_id, run_id)
            REFERENCES search_plans(id, run_id),
          FOREIGN KEY(plan_query_id, run_id)
            REFERENCES search_plan_queries(id, run_id)
        );

        CREATE INDEX candidate_occurrences_candidate_idx
          ON candidate_occurrences(candidate_id);
        CREATE INDEX candidate_occurrences_plan_query_idx
          ON candidate_occurrences(plan_query_id);
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL from "
        "the pre-v11 recovery boundary or apply a forward repair migration"
    )
