"""Represent authoritative zero-token semantic fixtures without sentinels."""

from alembic import op

revision = "0037_not_invoked_token_source"
down_revision = "0036_run_performance_telemetry"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        ALTER TABLE run_performance_telemetry
          DROP CONSTRAINT IF EXISTS run_performance_telemetry_token_source_check;
        ALTER TABLE run_performance_telemetry
          ADD CONSTRAINT run_performance_telemetry_token_source_check
          CHECK (token_source IN ('endpoint', 'tokenizer', 'not_invoked', 'unavailable'));
        ALTER TABLE endpoint_usage_records
          DROP CONSTRAINT IF EXISTS endpoint_usage_records_source_check;
        ALTER TABLE endpoint_usage_records
          ADD CONSTRAINT endpoint_usage_records_source_check
          CHECK (source IN ('endpoint', 'tokenizer', 'not_invoked', 'unavailable'));
        """
    )


def downgrade():
    op.execute(
        """
        UPDATE run_performance_telemetry
           SET token_source = 'unavailable'
         WHERE token_source = 'not_invoked';
        DELETE FROM endpoint_usage_records WHERE source = 'not_invoked';
        ALTER TABLE run_performance_telemetry
          DROP CONSTRAINT IF EXISTS run_performance_telemetry_token_source_check;
        ALTER TABLE run_performance_telemetry
          ADD CONSTRAINT run_performance_telemetry_token_source_check
          CHECK (token_source IN ('endpoint', 'tokenizer', 'unavailable'));
        ALTER TABLE endpoint_usage_records
          DROP CONSTRAINT IF EXISTS endpoint_usage_records_source_check;
        ALTER TABLE endpoint_usage_records
          ADD CONSTRAINT endpoint_usage_records_source_check
          CHECK (source IN ('endpoint', 'tokenizer', 'unavailable'));
        """
    )
