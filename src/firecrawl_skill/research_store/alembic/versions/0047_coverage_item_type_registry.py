"""Adopt the canonical coverage_item_type registry boundary."""

from __future__ import annotations

from alembic import op

from firecrawl_skill.persisted_types import COVERAGE_ITEM_TYPE

revision = "0047_coverage_item_type_registry"
down_revision = "0046_exact_source_coverage_item"
branch_labels = None
depends_on = None


REGISTRY_VERSION = 2


def upgrade() -> None:
    """Fail closed unless PostgreSQL already matches registry version 2."""
    op.execute(COVERAGE_ITEM_TYPE.postgres_assertion_sql(REGISTRY_VERSION))


def downgrade() -> None:
    raise RuntimeError("Research workflow migrations are forward-only")
