"""Persist staged asset promotion and sealed completion membership."""

from pathlib import Path

from alembic import op

revision = "0040_asset_promotion_membership"
down_revision = "0039_index_checkpoint_guard"
branch_labels = None
depends_on = None


def upgrade():
    root = Path(__file__).resolve().parent
    for number in range(1, 6):
        sql = (root / f"0040_asset_promotion_membership_{number}.sql").read_text(
            encoding="utf-8"
        )
        op.execute(sql)


def downgrade():
    raise RuntimeError(
        "Asset-promotion migrations are forward-only; apply a forward repair "
        "or restore PostgreSQL from backup"
    )
