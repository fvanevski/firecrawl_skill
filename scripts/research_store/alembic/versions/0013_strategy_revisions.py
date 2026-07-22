"""Add strategy_revisions table for proposal authorization.

This migration introduces the ``strategy_revisions`` table that persists
strategy-revision proposals and deterministic authorization decisions.
It powers issue #24 (strategy revision and deterministic authorization).

Key invariants enforced by DDL:

* strategy_revisions is append-only (no UPDATE/DELETE).
* Idempotency keys prevent duplicate proposals and decisions.
* Foreign keys link proposals and decisions to research_runs.
* Decision outcome is constrained to accepted/rejected.
* Rejection reasons are stored as a text array for auditability.

The revision is additive. It does not rewrite existing workflow,
coverage, spec, budget, corpus, or search records.

## Schema

### strategy_revisions

Stores both proposals and authorization decisions in a single
append-only table. Each row is either a proposal or a decision,
distinguished by ``row_type``.

- ``proposal`` rows: capture the semantic authority's proposed actions.
- ``decision`` rows: capture the deterministic policy's authorization
  outcome (accepted/rejected) with rejection reasons.

### Indexes

- ``strategy_revisions_run_cursor`` — ordered by (run_id, revision_order, id)
  for deterministic replay of the full proposal/decision history.
- ``strategy_revisions_proposal_idx`` — filter proposals by run and
  proposal_id.
- ``strategy_revisions_decision_idx`` — filter decisions by run,
  proposal_id, and outcome.
- ``strategy_revisions_idempotency`` — unique constraint on
  (run_id, idempotency_key) for deduplication.

### Compatibility

* No changes to existing Phase 1 or Phase 2 tables.
* ``research_runs`` is NOT extended — the current coverage revision
  on ``research_runs`` is read-only for this migration.
* The ``strategy_revisions`` table is independent; it can be dropped
  and recreated from PostgreSQL WAL if needed.
"""

from alembic import op


revision = "0013_strategy_revisions"
down_revision = "0012_coverage_events"
branch_labels = None
depends_on = None


def upgrade():
    # ----------------------------------------------------------------
    # strategy_revisions (append-only proposal + decision ledger)
    # ----------------------------------------------------------------

    op.execute(
        """
        CREATE TYPE strategy_revision_row_type AS ENUM (
          'proposal',
          'decision'
        );

        CREATE TYPE strategy_decision_type AS ENUM (
          'search',
          'scrape',
          'retrieve',
          'synthesize',
          'stop_partial',
          'stop_failed'
        );

        CREATE TYPE strategy_decision_outcome AS ENUM (
          'accepted',
          'rejected'
        );

        CREATE TABLE strategy_revisions (
          id                          uuid NOT NULL DEFAULT gen_random_uuid(),
          run_id                      uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,

          -- Revision tracking
          run_revision                bigint NOT NULL DEFAULT 0,
          coverage_revision           bigint NOT NULL DEFAULT 0,
          revision_order              bigint NOT NULL DEFAULT 0,

          -- Row type: proposal or decision
          row_type                    strategy_revision_row_type NOT NULL,

          -- Proposal fields (populated when row_type = 'proposal')
          proposal_id                 uuid NOT NULL,
          decision_type               strategy_decision_type,
          target_coverage_item_ids    jsonb NOT NULL DEFAULT '[]',
          proposed_queries            jsonb NOT NULL DEFAULT '[]',
          proposed_candidate_ids      jsonb NOT NULL DEFAULT '[]',
          proposed_retrieval_queries  jsonb NOT NULL DEFAULT '[]',
          expected_contribution       text NOT NULL DEFAULT '',
          estimated_cost              jsonb NOT NULL DEFAULT '{}',
          rationale                   text NOT NULL DEFAULT '',
          confidence                  double precision NOT NULL DEFAULT 0,

          -- Decision fields (populated when row_type = 'decision')
          decision_id                 uuid,
          outcome                     strategy_decision_outcome,
          rejection_reasons           text[] NOT NULL DEFAULT '{}',
          policy_version              text NOT NULL DEFAULT '',
          scope_expansion_type        text,
          scope_expansion_rationale   text,
          scope_expansion_approved    boolean,
          authorized_by               text NOT NULL DEFAULT '',

          -- Audit
          idempotency_key             text NOT NULL,
          actor_type                  text NOT NULL DEFAULT 'system',
          actor_identifier            text,
          created_at                  timestamptz NOT NULL DEFAULT now(),

          PRIMARY KEY (id),
          CONSTRAINT uk_strategy_revisions_idempotency
            UNIQUE (run_id, idempotency_key),
          CONSTRAINT chk_strategy_revisions_run_revision
            CHECK (run_revision >= 0),
          CONSTRAINT chk_strategy_revisions_coverage_revision
            CHECK (coverage_revision >= 0),
          CONSTRAINT chk_strategy_revisions_revision_order
            CHECK (revision_order > 0),
          CONSTRAINT chk_strategy_revisions_confidence
            CHECK (confidence >= 0 AND confidence <= 1),
          CONSTRAINT chk_strategy_revisions_row_type_match
            CHECK (
              (row_type = 'proposal' AND decision_id IS NULL) OR
              (row_type = 'decision' AND proposal_id IS NOT NULL)
            )
        );

        -- Indexes
        CREATE INDEX strategy_revisions_run_cursor_idx
          ON strategy_revisions (run_id, revision_order, id);
        CREATE INDEX strategy_revisions_proposal_idx
          ON strategy_revisions (run_id, proposal_id);
        CREATE INDEX strategy_revisions_decision_idx
          ON strategy_revisions (run_id, proposal_id, outcome);
        CREATE INDEX strategy_revisions_decision_outcome_idx
          ON strategy_revisions (run_id, outcome);

        -- Append-only trigger
        CREATE OR REPLACE FUNCTION _strategy_revisions_append_only()
        RETURNS trigger AS $$
        BEGIN
          IF TG_OP IN ('UPDATE', 'DELETE') THEN
            RAISE EXCEPTION 'strategy_revisions is append-only';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER strategy_revisions_append_only_trigger
          BEFORE UPDATE OR DELETE ON strategy_revisions
          FOR EACH ROW EXECUTE FUNCTION _strategy_revisions_append_only();
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v13 recovery boundary or apply a forward repair "
        "migration."
    )
