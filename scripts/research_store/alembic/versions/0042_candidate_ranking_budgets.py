"""Add candidate ranking provenance and fail-closed corpus-budget audit tables.

No historical rankings, budget decisions, or overrides are synthesized. Existing
rows remain exactly as persisted; issue #215 begins recording authoritative
policy evidence only for operations executed after this migration.

The revision is additive and forward-only. It does not rewrite corpus,
snapshot, derivation, index, job, lease, or lifecycle records.
"""

from alembic import op

revision = "0042_candidate_ranking_budgets"
down_revision = "0041_search_provenance"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        r"""
        CREATE TABLE candidate_rankings(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id),
          search_response_id uuid NOT NULL,
          invocation_id uuid NOT NULL,
          candidate_id uuid NOT NULL,
          source_rank integer NOT NULL CHECK(source_rank > 0),
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
          expected_char_count integer CHECK(expected_char_count IS NULL OR expected_char_count >= 0),
          size_penalty double precision NOT NULL CHECK(size_penalty >= 0 AND size_penalty <= 1),
          total_score double precision NOT NULL CHECK(total_score >= 0 AND total_score <= 1),
          rationale text NOT NULL,
          decision text NOT NULL CHECK(decision IN ('selected','rejected')),
          selected_ordinal integer,
          decision_reason text NOT NULL,
          content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
          created_at timestamptz NOT NULL DEFAULT now(),
          CHECK(
            (decision='selected' AND selected_ordinal IS NOT NULL AND selected_ordinal >= 0)
            OR (decision='rejected' AND selected_ordinal IS NULL)
          ),
          FOREIGN KEY(search_response_id,run_id)
            REFERENCES search_responses(id,run_id) ON DELETE RESTRICT,
          FOREIGN KEY(invocation_id,run_id)
            REFERENCES research_invocations(id,run_id) ON DELETE RESTRICT,
          FOREIGN KEY(candidate_id,run_id)
            REFERENCES search_candidates(id,run_id) ON DELETE RESTRICT,
          UNIQUE(invocation_id,candidate_id)
        );
        CREATE INDEX candidate_rankings_run_idx
          ON candidate_rankings(run_id,created_at,id);
        CREATE INDEX candidate_rankings_response_idx
          ON candidate_rankings(search_response_id,total_score DESC,source_rank);
        CREATE INDEX candidate_rankings_decision_idx
          ON candidate_rankings(run_id,decision,total_score DESC);

        CREATE TABLE corpus_budget_checks(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id),
          invocation_id uuid,
          lifecycle_revision bigint,
          phase text NOT NULL CHECK(phase IN (
            'pre_extraction','post_extraction','completion_admission'
          )),
          candidate_count integer NOT NULL CHECK(candidate_count >= 0),
          total_bytes bigint NOT NULL CHECK(total_bytes >= 0),
          total_chunks integer NOT NULL CHECK(total_chunks >= 0),
          generic_page_count integer NOT NULL CHECK(generic_page_count >= 0),
          extraction_attempts integer NOT NULL CHECK(extraction_attempts >= 0),
          per_asset_chunk_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
          scope jsonb NOT NULL DEFAULT '{}'::jsonb,
          budget jsonb NOT NULL,
          accepted_without_override boolean NOT NULL,
          hard_violations jsonb NOT NULL DEFAULT '[]'::jsonb,
          soft_violations jsonb NOT NULL DEFAULT '[]'::jsonb,
          content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
          created_at timestamptz NOT NULL DEFAULT now(),
          CHECK(generic_page_count <= candidate_count),
          FOREIGN KEY(invocation_id,run_id)
            REFERENCES research_invocations(id,run_id) ON DELETE RESTRICT,
          UNIQUE(id,run_id),
          UNIQUE(run_id,phase,content_sha256)
        );
        CREATE INDEX corpus_budget_checks_run_idx
          ON corpus_budget_checks(run_id,created_at,id);
        CREATE INDEX corpus_budget_checks_invocation_idx
          ON corpus_budget_checks(invocation_id,phase)
          WHERE invocation_id IS NOT NULL;

        CREATE TABLE budget_override_justifications(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          budget_check_id uuid NOT NULL,
          run_id uuid NOT NULL REFERENCES research_runs(id),
          limit_name text NOT NULL CHECK(length(btrim(limit_name)) > 0),
          reason text NOT NULL CHECK(length(btrim(reason)) > 0),
          author text NOT NULL CHECK(length(btrim(author)) > 0),
          content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
          created_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY(budget_check_id,run_id)
            REFERENCES corpus_budget_checks(id,run_id) ON DELETE RESTRICT,
          UNIQUE(budget_check_id,content_sha256)
        );
        CREATE INDEX budget_override_justifications_check_idx
          ON budget_override_justifications(budget_check_id,created_at,id);
        CREATE INDEX budget_override_justifications_run_idx
          ON budget_override_justifications(run_id,created_at,id);

        CREATE FUNCTION reject_candidate_policy_evidence_change()
        RETURNS trigger LANGUAGE plpgsql AS $function$
        BEGIN
          RAISE EXCEPTION 'candidate policy evidence is append-only'
            USING ERRCODE='55000';
        END;
        $function$;
        CREATE TRIGGER candidate_rankings_append_only_trigger
          BEFORE UPDATE OR DELETE ON candidate_rankings
          FOR EACH ROW EXECUTE FUNCTION reject_candidate_policy_evidence_change();
        CREATE TRIGGER corpus_budget_checks_append_only_trigger
          BEFORE UPDATE OR DELETE ON corpus_budget_checks
          FOR EACH ROW EXECUTE FUNCTION reject_candidate_policy_evidence_change();
        CREATE TRIGGER budget_override_justifications_append_only_trigger
          BEFORE UPDATE OR DELETE ON budget_override_justifications
          FOR EACH ROW EXECUTE FUNCTION reject_candidate_policy_evidence_change();
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; apply a forward repair "
        "for candidate ranking budgets or restore PostgreSQL from backup"
    )
