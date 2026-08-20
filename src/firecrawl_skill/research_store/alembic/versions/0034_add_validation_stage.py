"""Add ``validation`` to synthesis_stages stage_name CHECK constraint.

This migration resolves the P0 defect identified in the Phase 7 release gate
evaluation (issue #70).  Migration 0031 created the ``synthesis_stages`` table
with a CHECK constraint that only allowed ``("outline", "binding", "draft",
"citation_pass")``.  The autonomous-local synthesis pipeline defines a fifth
deterministic stage — ``validation`` — which is referenced throughout the
``LocalSynthesisService`` and ``ReportValidator`` code but could never be
persisted because the database constraint rejected it.

This migration adds ``"validation"`` to the allowed stage names so that the
validation stage can be persisted in production.

## Schema changes

### synthesis_stages

* ``stage_name`` CHECK constraint expanded to include ``"validation"``.

## Design decisions

* Forward-only migration.  No data is written to existing runs.
* The ``synthesis_stages`` table is modified in place; no new table is created.
* The downgrade path is a no-op guard — dropping the constraint would lose
  the validation stage allowance and could break existing deployments that
  have already persisted validation records.  Downgrade is intentionally
  unsupported.

## Compatibility

* Forward-only migration.  No data is written to existing runs.
* No existing tables are modified other than the ``synthesis_stages`` CHECK
  constraint itself.
* Phase 1–6 data is preserved.
"""

from alembic import op

revision = "0034_add_validation_stage"
down_revision = "0033_resource_governance"
branch_labels = None
depends_on = None

STAGE_NAMES = ("outline", "binding", "draft", "citation_pass", "validation")


def upgrade():
    # Drop the old CHECK constraint and add the new one with "validation".
    op.execute(
        f"""
        ALTER TABLE synthesis_stages
          DROP CONSTRAINT IF EXISTS synthesis_stages_stage_name_check;

        ALTER TABLE synthesis_stages
          ADD CONSTRAINT synthesis_stages_stage_name_check
            CHECK (stage_name IN {tuple(STAGE_NAMES)});
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v34 recovery boundary or apply a forward repair "
        "migration."
    )
