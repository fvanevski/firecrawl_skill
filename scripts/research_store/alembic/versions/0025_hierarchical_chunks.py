"""Hierarchical chunking: tokenizer name and parent block columns.

This migration adds:

* ``tokenizer_name`` column to ``chunks`` — records which tokenizer was
  used to count tokens for this chunk.
* ``parent_block_id`` column to ``chunks`` — nullable FK to
  ``document_blocks`` that identifies the parent block this chunk derives
  from. For multi-block chunks this points to the first block.
* Updated ``chunks_derivation_key`` constraint to include
  ``tokenizer_name`` in addition to ``chunker_name`` and ``chunker_version``
  so that tokenizer upgrades produce new derivations.

.. versionadded:: P5-06
   Tokenizer-backed hierarchical chunking.
"""

from alembic import op

revision = "0025_hierarchical_chunks"
down_revision = "0024_normalized_blocks_and_transformations"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Add tokenizer_name column (nullable for existing chunks)
    op.add_column(
        "chunks",
        op.Column(
            "tokenizer_name",
            op.Text(),
            nullable=True,
        ),
    )

    # 2. Add parent_block_id column (nullable FK to document_blocks)
    op.add_column(
        "chunks",
        op.Column(
            "parent_block_id",
            op.UUID(),
            nullable=True,
        ),
    )

    # 3. Add FK constraint
    op.create_foreign_key(
        "chunks_parent_block_id_fkey",
        "chunks",
        "document_blocks",
        ["parent_block_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # 4. Update derivation key constraint to include tokenizer_name
    # Drop old constraint
    op.execute("ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_derivation_key")

    # Add new constraint with tokenizer_name
    op.execute(
        """
        ALTER TABLE chunks ADD CONSTRAINT chunks_derivation_key
          UNIQUE(document_id, chunker_name, chunker_version, tokenizer_name, ordinal)
        """
    )

    # 5. Set default tokenizer_name for existing chunks
    op.execute(
        "UPDATE chunks SET tokenizer_name = 'cl100k_base' WHERE tokenizer_name IS NULL"
    )

    # 6. Create index on parent_block_id for efficient lookups
    op.create_index(
        "chunks_parent_block_idx",
        "chunks",
        ["parent_block_id"],
    )

    # 7. Create index on tokenizer_name for filtering
    op.create_index(
        "chunks_tokenizer_idx",
        "chunks",
        ["tokenizer_name"],
    )


def downgrade():
    # Remove new indexes
    op.execute("DROP INDEX IF EXISTS chunks_parent_block_idx")
    op.execute("DROP INDEX IF EXISTS chunks_tokenizer_idx")

    # Drop FK
    op.execute(
        "ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_parent_block_id_fkey"
    )

    # Drop new derivation key and restore old one
    op.execute("ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_derivation_key")
    op.execute(
        """
        ALTER TABLE chunks ADD CONSTRAINT chunks_derivation_key
          UNIQUE(document_id, chunker_name, chunker_version, ordinal)
        """
    )

    # Drop columns
    op.drop_column("chunks", "parent_block_id")
    op.drop_column("chunks", "tokenizer_name")
