"""Add parser_version to document_blocks.

Revision ID: 0023_parser_version
Revises: 0022_extraction_attempt_linkage
Create Date: 2026-07-23

"""
from alembic import op


revision = "0023_parser_version"
down_revision = "0022_extraction_attempt_linkage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE document_blocks
        ADD COLUMN IF NOT EXISTS parser_version TEXT NOT NULL DEFAULT 'canonical-v1';
        """
    )


def downgrade() -> None:
    pass
