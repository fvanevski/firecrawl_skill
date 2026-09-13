"""Add the exact canonical-source coverage item type.

Issue #375 makes exact canonical-source authority a first-class coverage
obligation.  The domain and coverage services therefore persist
``exact_source_requirement`` coverage items; extend the PostgreSQL enum before
those rows can be written.

The migration is additive and forward-only.  Existing coverage events retain
their original enum values unchanged.
"""

from alembic import op

revision = "0046_exact_source_coverage_item"
down_revision = "0045_operator_actions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        ALTER TYPE coverage_item_type
          ADD VALUE IF NOT EXISTS 'exact_source_requirement'
          AFTER 'source_requirement';
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; coverage_item_type enum "
        "expansion requires restoring PostgreSQL from the pre-0046 recovery "
        "boundary or applying a forward repair migration."
    )
