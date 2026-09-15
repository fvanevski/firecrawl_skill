"""Admit durable semantic-resolution operator actions.

Issue #386 adds a representable human-resolution boundary for semantic
ambiguity. Existing operator-action rows retain their original kinds and
immutability semantics; this migration only expands the table check contract.
"""

from alembic import op

revision = "0048_semantic_resolution_action"
down_revision = "0047_coverage_item_type_registry"
branch_labels = None
depends_on = None


_ACTION_KIND_CHECK = "operator_actions_action_kind_check"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE operator_actions
          DROP CONSTRAINT {_ACTION_KIND_CHECK};
        ALTER TABLE operator_actions
          ADD CONSTRAINT {_ACTION_KIND_CHECK} CHECK (
            action_kind IN (
              'candidate_budget_authorization',
              'curation_selection_required',
              'material_scope_change_required',
              'semantic_resolution_required',
              'manual_environment_resolution'
            )
          );
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "Research workflow migrations are forward-only; semantic operator-action "
        "authority requires restoring PostgreSQL from the pre-0048 recovery "
        "boundary or applying a forward repair migration."
    )
