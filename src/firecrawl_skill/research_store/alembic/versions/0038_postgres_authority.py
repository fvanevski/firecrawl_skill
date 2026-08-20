"""Add resource-sample completeness columns (issue #170).

This migration adds columns to ``run_resource_samples`` required for
authoritative telemetry completeness:

* ``failure_reason`` — explicit reason when status is not ``"measured"``.
* ``window_start`` — ISO-8601 timestamp when the workload window began.
* ``window_end`` — ISO-8601 timestamp when the workload window ended.
* ``sampling_interval_seconds`` — interval between samples in the workload window.

## Design decisions

* All new columns are nullable so existing rows remain valid.
* ``failure_reason`` is a free-text field — no CHECK constraint — because
  reasons vary by collector (psutil, pynvml, driver errors, etc.).
* ``window_start`` and ``window_end`` are ISO-8601 strings for consistency
  with ``sample_at``.
* ``sampling_interval_seconds`` is a double precision number for the fixed
  interval used during sampling.

## Authority boundary

* This revision is the clean PostgreSQL-only schema head. Deployments using an
  earlier schema must reset the disposable research datastore before use.
"""

from alembic import op

revision = "0038_postgres_authority"
down_revision = "0037_not_invoked_token_source"
branch_labels = None
depends_on = None


def upgrade():
    # Make value column nullable — samples with status != 'measured' may have no value.
    op.execute("ALTER TABLE run_resource_samples ALTER COLUMN value DROP NOT NULL")

    op.execute(
        """
        ALTER TABLE run_resource_samples
          ADD COLUMN failure_reason      text,
          ADD COLUMN window_start         text,
          ADD COLUMN window_end           text,
          ADD COLUMN sampling_interval_seconds double precision;
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v37 recovery boundary or apply a forward repair "
        "migration."
    )
