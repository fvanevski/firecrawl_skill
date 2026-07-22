"""Add terminal_decisions table for deterministic terminal decisions.

This migration introduces the ``terminal_decisions`` table that persists
terminal decisions produced by the ``TerminalDecisionPolicy``.  It powers
issue #27 (terminal rules, loop detection, and blocked-state handling).

Key invariants enforced by DDL:

* terminal_decisions is append-only (no UPDATE/DELETE).
* Idempotency keys prevent duplicate decisions for the same run.
* Foreign keys link decisions to research_runs.
* Outcome is constrained to the known terminal outcomes.
* No-progress signals are stored as a text array for auditability.

The revision is additive. It does not rewrite existing workflow,
coverage, spec, budget, corpus, search, or strategy records.

## Schema

### terminal_decisions

- ``decision_id`` — unique identifier for the terminal decision.
- ``run_id`` — the research run this decision applies to.
- ``run_revision`` — the run lifecycle revision at decision time.
- ``coverage_revision`` — the coverage revision at decision time.
- ``outcome`` — one of sufficient, partial, blocked, failed, cancelled.
- ``no_progress_signals`` — array of deterministic no-progress signals.
- ``unresolved_gap`` — human-readable description of the evidence gap.
- ``policy_version`` — the policy version that produced the decision.
- ``idempotency_key`` — deduplication key (run_id + key must be unique).

### Indexes

- ``terminal_decisions_run_cursor`` — ordered by (run_id, created_at)
  for deterministic replay of the full terminal-decision history.
- ``terminal_decisions_outcome_idx`` — filter decisions by outcome.
- ``terminal_decisions_idempotency`` — unique constraint on
  (run_id, idempotency_key) for deduplication.

### Compatibility

* No changes to existing Phase 1, 2, or 3 tables.
* The ``terminal_decisions`` table is independent; it can be dropped
  and recreated from PostgreSQL WAL if needed.
* Existing orchestrator behavior is preserved — this table is for
  observability and audit of terminal decisions.
"""

from alembic import op


revision = "0015_terminal_decisions"
down_revision = "0014_coverage_event_types"
branch_labels = None
depends_on = None


def upgrade():
    # ----------------------------------------------------------------
    # terminal_decisions (append-only terminal-decision ledger)
    # ----------------------------------------------------------------

    op.execute(
        """
        CREATE TYPE terminal_decision_outcome AS ENUM (
          'sufficient',
          'partial',
          'blocked',
          'failed',
          'cancelled'
        );

        CREATE TABLE terminal_decisions (
          id                          uuid NOT NULL DEFAULT gen_random_uuid(),
          run_id                      uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,

          -- Decision metadata
          decision_id                 uuid NOT NULL,
          run_revision                bigint NOT NULL DEFAULT 0,
          coverage_revision           bigint NOT NULL DEFAULT 0,
          outcome                     terminal_decision_outcome NOT NULL,
          no_progress_signals         text[] NOT NULL DEFAULT '{}',
          unresolved_gap              text NOT NULL DEFAULT '',
          policy_version              text NOT NULL DEFAULT 'terminal-decision-policy-v1',

          -- Audit
          idempotency_key             text NOT NULL,
          created_at                  timestamptz NOT NULL DEFAULT now(),

          PRIMARY KEY (id),
          CONSTRAINT uk_terminal_decisions_idempotency
            UNIQUE (run_id, idempotency_key),
          CONSTRAINT chk_terminal_decisions_run_revision
            CHECK (run_revision >= 0),
          CONSTRAINT chk_terminal_decisions_coverage_revision
            CHECK (coverage_revision >= 0)
        );

        -- Indexes
        CREATE INDEX terminal_decisions_run_cursor_idx
          ON terminal_decisions (run_id, created_at, id);
        CREATE INDEX terminal_decisions_outcome_idx
          ON terminal_decisions (run_id, outcome);
        CREATE INDEX terminal_decisions_decision_idx
          ON terminal_decisions (run_id, decision_id);

        -- Append-only trigger
        CREATE OR REPLACE FUNCTION _terminal_decisions_append_only()
        RETURNS trigger AS $$
        BEGIN
          IF TG_OP IN ('UPDATE', 'DELETE') THEN
            RAISE EXCEPTION 'terminal_decisions is append-only';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER terminal_decisions_append_only_trigger
          BEFORE UPDATE OR DELETE ON terminal_decisions
          FOR EACH ROW EXECUTE FUNCTION _terminal_decisions_append_only();
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v15 recovery boundary or apply a forward repair "
        "migration."
    )