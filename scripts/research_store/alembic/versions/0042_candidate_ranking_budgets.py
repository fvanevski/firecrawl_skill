"""Add candidate ranking scores and corpus budget enforcement tables.

Historical rows are backfilled only when the search response backend proves a
same-run invocation with a positive provider-attempt ordinal. Ambiguous or
malformed history remains explicitly unresolved.

The revision is additive. It does not rewrite corpus, snapshot, derivation,
index, job, lease, provenance, or existing workflow records.
"""

from alembic import op

revision = "0042_candidate_ranking_budgets"
down_revision = "0041_search_provenance"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        r"""
        -- URL-type classification and ranking score per candidate.
        CREATE TABLE candidate_rankings(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id),
          candidate_id uuid NOT NULL,
          url text NOT NULL,
          url_type text NOT NULL CHECK(url_type IN (
            'article','live_blog','official_release','topic_hub',
            'home_page','reference_page','search_page','unknown'
          )),
          base_score double precision NOT NULL CHECK(base_score >= 0 AND base_score <= 1),
          url_type_penalty double precision NOT NULL CHECK(url_type_penalty >= 0 AND url_type_penalty <= 1),
          freshness_status text NOT NULL CHECK(freshness_status IN (
            'satisfied','unsatisfied','uncertain','not_applicable'
          )),
          freshness_penalty double precision NOT NULL CHECK(freshness_penalty >= 0 AND freshness_penalty <= 1),
          is_duplicate boolean NOT NULL,
          duplication_penalty double precision NOT NULL CHECK(duplication_penalty >= 0 AND duplication_penalty <= 1),
          expected_char_count integer,
          size_penalty double precision NOT NULL CHECK(size_penalty >= 0 AND size_penalty <= 1),
          total_score double precision NOT NULL CHECK(total_score >= 0 AND total_score <= 1),
          rationale text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(run_id, candidate_id)
        );
        CREATE INDEX candidate_rankings_run_idx
          ON candidate_rankings(run_id, created_at);
        CREATE INDEX candidate_rankings_score_idx
          ON candidate_rankings(run_id, total_score DESC);

        -- Corpus budget check results per run.
        CREATE TABLE corpus_budget_checks(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id),
          candidate_count integer NOT NULL CHECK(candidate_count >= 0),
          total_bytes bigint NOT NULL CHECK(total_bytes >= 0),
          total_chunks integer NOT NULL CHECK(total_chunks >= 0),
          generic_page_count integer NOT NULL CHECK(generic_page_count >= 0),
          extraction_attempts integer NOT NULL CHECK(extraction_attempts >= 0),
          accepted boolean NOT NULL,
          hard_violations jsonb NOT NULL DEFAULT '[]'::jsonb,
          soft_violations jsonb NOT NULL DEFAULT '[]'::jsonb,
          content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(run_id, content_sha256)
        );
        CREATE INDEX corpus_budget_checks_run_idx
          ON corpus_budget_checks(run_id, created_at);

        -- Override justifications for soft budget limits.
        CREATE TABLE budget_override_justifications(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id),
          limit_name text NOT NULL,
          reason text NOT NULL,
          author text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
          UNIQUE(run_id, limit_name, author, created_at)
        );
        CREATE INDEX budget_override_justifications_run_idx
          ON budget_override_justifications(run_id, created_at);
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; apply a forward repair "
        "for candidate ranking budgets or restore PostgreSQL from backup"
    )
