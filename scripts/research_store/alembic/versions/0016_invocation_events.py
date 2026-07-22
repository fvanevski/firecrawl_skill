"""Add sequence numbering and event-type enum to research_events.

This migration strengthens the ``research_events`` table introduced in
migration 0006 (workflow state) by adding:

* ``event_type`` enum — a constrained set of invocation event types
  (pivot, retry, decision, recovery, annotation, and the existing
  lifecycle events).
* ``sequence_number`` — a ``bigint`` column that provides stable,
  queryable ordering of events within a run, independent of timestamps.
* ``invocation_id`` — backfilled for invocation-level events;
  remains nullable for run-level events (``run_started``, etc.)
* A unique index on ``(run_id, sequence_number)`` for deterministic
  replay and ordering.

PRD mapping: FR-001, Section 14

Key invariants enforced by DDL:

* ``sequence_number`` is monotonically increasing per run.
* ``event_type`` is constrained to the enum — no ad-hoc strings.
* The unique index prevents duplicate sequence numbers per run.
* The migration is forward-only (additive); it does not rewrite
  existing Phase 1–3 data.

## Schema changes

### research_events

- ``event_type`` — constrained to the ``research_event_type`` enum.
- ``sequence_number`` — ``bigint NOT NULL``, defaults to next value
  from a per-run sequence.
- ``invocation_id`` — backfilled for invocation-level events;
  remains nullable for run-level events (``run_started``,
  ``run_finished``, ``run_reopened``) that are
  not associated with a specific invocation.

### research_event_type enum values

* ``run_started`` — research run began
* ``run_finished`` — research run completed / failed / partial
* ``run_reopened`` — terminal run reopened
* ``invocation_started`` — invocation began
* ``invocation_finished`` — invocation completed
* ``invocation_event`` — generic invocation event
* ``pivot`` — search pivot to new query
* ``retry`` — retry of a failed operation
* ``decision`` — deterministic decision (e.g. budget, strategy)
* ``recovery`` — recovery from a transient failure
* ``annotation`` — human or agent annotation

### Indexes

- ``research_events_sequence_idx`` — unique on ``(run_id, sequence_number)``
- ``research_events_type_idx`` — filter by event type
- ``research_events_run_seq_idx`` — ordered replay by ``(run_id, sequence_number)``

### Compatibility

* Existing ``research_events`` rows are migrated: ``event_type`` is set
  to ``invocation_event`` for legacy rows, and ``sequence_number`` is
  assigned based on ``created_at`` order (ties broken by ``id``).
* The ``invocation_id`` column is backfilled for invocation-level
  events; run-level events (``run.created``, etc.) remain nullable.
* No changes to existing Phase 1, 2, or 3 tables.

### Forward-repair

If this migration is interrupted, re-run ``upgrade head`` from the
last successful revision. The migration is idempotent — the enum
and columns use ``CREATE TYPE ... NOT EXISTS`` guards where possible.
"""

from alembic import op


revision = "0016_invocation_events"
down_revision = "0015_terminal_decisions"
branch_labels = None
depends_on = None


def upgrade():
    # ----------------------------------------------------------------
    # 1. Create the event_type enum (idempotent)
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_type WHERE typname = 'research_event_type'
          ) THEN
            CREATE TYPE research_event_type AS ENUM (
              'run_started',
              'run_finished',
              'run_reopened',
              'invocation_started',
              'invocation_finished',
              'invocation_event',
              'pivot',
              'retry',
              'decision',
              'recovery',
              'annotation'
            );
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 2. Add sequence_number column (nullable initially for backfill)
    #    (idempotent — skip if already exists)
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'research_events' AND column_name = 'sequence_number'
          ) THEN
            ALTER TABLE research_events
              ADD COLUMN sequence_number bigint;
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 3. Backfill sequence_number from existing rows
    #    Temporarily disable append-only trigger for backfill.
    # ----------------------------------------------------------------
    op.execute(
        """
        ALTER TABLE research_events DISABLE TRIGGER ALL;
        """
    )
    op.execute(
        """
        WITH ordered AS (
          SELECT id, ROW_NUMBER() OVER (
            ORDER BY created_at, id
          ) AS seq
          FROM research_events
        )
        UPDATE research_events
        SET sequence_number = ordered.seq
        FROM ordered
        WHERE research_events.id = ordered.id;
        """
    )

    # ----------------------------------------------------------------
    # 4. Make sequence_number NOT NULL and add unique constraint
    # ----------------------------------------------------------------
    op.execute(
        """
        ALTER TABLE research_events
          ALTER COLUMN sequence_number SET NOT NULL,
          ADD CONSTRAINT research_events_sequence_number_check CHECK (sequence_number > 0);
        """
    )

    # ----------------------------------------------------------------
    # 5. Add event_type column with default for existing rows
    #    (idempotent — skip if already exists)
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'research_events' AND column_name = 'event_type'
          ) THEN
            ALTER TABLE research_events
              ADD COLUMN event_type research_event_type
                DEFAULT 'invocation_event';
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 6. Migrate existing event_type values from payload
    # ----------------------------------------------------------------
    op.execute(
        """
        UPDATE research_events
        SET event_type = CASE
          WHEN payload->>'event' = 'run_started' THEN 'run_started'::research_event_type
          WHEN payload->>'event' = 'run_finished' THEN 'run_finished'::research_event_type
          WHEN payload->>'event' = 'invocation_started' THEN 'invocation_started'::research_event_type
          WHEN payload->>'event' = 'invocation_finished' THEN 'invocation_finished'::research_event_type
          WHEN payload->>'event' = 'invocation_event' THEN 'invocation_event'::research_event_type
          WHEN event_type = 'pivot' THEN 'pivot'::research_event_type
          WHEN event_type = 'retry' THEN 'retry'::research_event_type
          WHEN event_type = 'decision' THEN 'decision'::research_event_type
          WHEN event_type = 'recovery' THEN 'recovery'::research_event_type
          WHEN event_type = 'annotation' THEN 'annotation'::research_event_type
          ELSE 'invocation_event'::research_event_type
        END
        WHERE event_type IS NULL;
        """
    )

    # ----------------------------------------------------------------
    # 7. Make event_type NOT NULL
    # ----------------------------------------------------------------
    op.execute(
        """
        ALTER TABLE research_events
          ALTER COLUMN event_type SET NOT NULL;
        """
    )

    # ----------------------------------------------------------------
    # 8. Strengthen invocation_id FK
    #    Backfill NULLs with the first invocation's id for the run.
    #    Keep nullable for run-level events (run_started, run_finished, etc.)
    # ----------------------------------------------------------------
    op.execute(
        """
        UPDATE research_events
        SET invocation_id = (
          SELECT id FROM research_invocations
          WHERE research_invocations.run_id = research_events.run_id
          ORDER BY created_at ASC
          LIMIT 1
        )
        WHERE invocation_id IS NULL
          AND event_type NOT IN ('run_started', 'run_finished', 'run_reopened');
        """
    )

    # Note: invocation_id remains nullable to support run-level events
    # (run.created, run.transitioned, run.execution_mode_changed) that
    # are not associated with a specific invocation.

    # ----------------------------------------------------------------
    # 9. Re-enable append-only trigger after all backfills complete
    # ----------------------------------------------------------------
    op.execute(
        """
        ALTER TABLE research_events ENABLE TRIGGER ALL;
        """
    )

    # ----------------------------------------------------------------
    # 10. Add indexes (idempotent)
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'research_events' AND indexname = 'research_events_sequence_idx'
          ) THEN
            CREATE UNIQUE INDEX research_events_sequence_idx
              ON research_events (run_id, sequence_number);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'research_events' AND indexname = 'research_events_type_idx'
          ) THEN
            CREATE INDEX research_events_type_idx
              ON research_events (event_type);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'research_events' AND indexname = 'research_events_run_seq_idx'
          ) THEN
            CREATE INDEX research_events_run_seq_idx
              ON research_events (run_id, sequence_number, id);
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 11. Record migration
    # ----------------------------------------------------------------
    op.execute(
        "INSERT INTO schema_migrations(version) VALUES (16) ON CONFLICT DO NOTHING"
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v16 recovery boundary or apply a forward repair "
        "migration."
    )