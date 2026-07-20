"""Initial authoritative research asset store."""

from pathlib import Path
from alembic import op

revision = "0001_research_store"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    sql = Path(__file__).parents[2] / "migrations" / "001_initial.sql"
    op.get_bind().exec_driver_sql(sql.read_text(encoding="utf-8"))


def downgrade():
    raise RuntimeError(
        "Research corpus migrations are forward-only; restore PostgreSQL from backup"
    )
