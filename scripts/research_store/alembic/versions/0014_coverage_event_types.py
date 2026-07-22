"""Extend coverage_event_type with workflow observation event types.

This migration adds new event types to the coverage_event_type enum that
represent objective workflow observations. These events record deterministic
facts (candidate identified, extraction attempted, asset acquired, evidence
retrieved, source class observed, freshness observed) and are deliberately
separate from semantic support judgments.

PRD mapping: FR-012, FR-013

Key distinctions:
* asset_acquired records that content was obtained — it does NOT imply
  that a claim is semantically supported.
* evidence_retrieved records passage retrieval — also not a support judgment.
* candidate_identified and extraction_attempted are process observations.
* source_class_observed and freshness_observed are provenance facts.

Independent-source counts are tracked via unique source URLs in the payload,
not by raw event counts. This migration adds the event types; the projection
logic in postgres.py handles the counting.

The downgrade is forward-only (raises RuntimeError), consistent with
migration 0012.
"""

from alembic import op

revision = "0014_coverage_event_types"
down_revision = "0013_strategy_revisions"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        -- Create the extended enum type
        CREATE TYPE coverage_event_type_new AS ENUM (
          'item_created',
          'item_status_changed',
          'item_gap_identified',
          'item_gap_resolved',
          'snapshot_created',
          'projection_rebuilt',
          'candidate_identified',
          'extraction_attempted',
          'asset_acquired',
          'evidence_retrieved',
          'source_class_observed',
          'freshness_observed'
        );

        -- Convert columns to use the new type
        ALTER TABLE coverage_events
          ALTER COLUMN event_type TYPE coverage_event_type_new
          USING event_type::text;

        -- Drop the old type and rename
        DROP TYPE coverage_event_type;
        ALTER TYPE coverage_event_type_new RENAME TO coverage_event_type;
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v14 recovery boundary or apply a forward repair "
        "migration."
    )
