"""Adopt the canonical coverage_item_type registry boundary."""

from __future__ import annotations

from alembic import op

from firecrawl_skill.persisted_types import COVERAGE_ITEM_TYPE

revision = "0047_coverage_item_type_registry"
down_revision = "0046_exact_source_coverage_item"
branch_labels = None
depends_on = None


REGISTRY_KEY = COVERAGE_ITEM_TYPE.key
FROM_REGISTRY_VERSION = 2
REGISTRY_VERSION = 2


def upgrade() -> None:
    """Adopt the registry contract at the already-materialized v2 projection."""
    for statement in COVERAGE_ITEM_TYPE.postgres_transition_sql(
        FROM_REGISTRY_VERSION, REGISTRY_VERSION
    ):
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Research workflow migrations are forward-only")
