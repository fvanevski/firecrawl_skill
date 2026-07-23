"""Add extraction_attempt_id linkage to authoritative tables.

Revision ID: 0022_extraction_attempt_linkage
Revises: 0021_extraction_attempts
Create Date: 2026-07-22

"""
from alembic import op
import sqlalchemy as sa


revision = "0022_extraction_attempt_linkage"
down_revision = "0021_extraction_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add extraction_attempt_id to asset_snapshots
    op.execute(
        """
        ALTER TABLE asset_snapshots
        ADD COLUMN IF NOT EXISTS extraction_attempt_id UUID REFERENCES extraction_attempts(id) ON DELETE SET NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_asset_snapshots_extraction_attempt
        ON asset_snapshots(extraction_attempt_id)
        WHERE extraction_attempt_id IS NOT NULL;
        """
    )

    # Add extraction_attempt_id to documents
    op.execute(
        """
        ALTER TABLE documents
        ADD COLUMN IF NOT EXISTS extraction_attempt_id UUID REFERENCES extraction_attempts(id) ON DELETE SET NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_documents_extraction_attempt
        ON documents(extraction_attempt_id)
        WHERE extraction_attempt_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    pass
