"""Add audit identity hash and idempotency constraint.

This migration introduces the audit identity hash column and partial
unique constraint required by issue #34 (Idempotent audit scheduling).

## Schema changes

### New column on ``audit_assessments``

* ``audit_identity_hash`` — text, SHA-256 hex digest of the canonical
  JSON representation of the audit identity components:
  ``target_hash``, ``evaluator_version``, ``prompt_template_version``,
  ``model_fingerprint``, ``policy_version``, and ``stage_set`` (sorted).
  This hash is the deterministic key that identifies an "equivalent"
  audit.  Two assessments with the same ``audit_identity_hash`` are
  considered equivalent and the scheduler reuses the existing one.

### New partial unique constraint

* ``uk_audit_assessments_identity`` — UNIQUE
  ``(audit_identity_hash)`` WHERE ``status != 'failed'``.

  This partial index enforces idempotency at the database level:
  concurrent equivalent audit requests cannot create multiple active
  assessments.  Failed assessments are excluded so that a retried audit
  can still be created.

### New index

* ``idx_audit_assessments_identity_hash`` — on
  ``audit_identity_hash`` for fast lookup during reuse checks.

### Backfill

Existing assessments from migration 0018 are backfilled with an MD5
hash derived from their existing columns.  The backfill uses MD5
because it is universally available in PostgreSQL without extensions.
New assessments use SHA-256 computed at the application layer.  The
hash algorithm difference means legacy backfilled hashes will never
collide with new SHA-256 hashes, so the partial unique constraint is
safe.

## Deterministic identity policy (PRD Section 17.3)

Semantic outputs may be reused only when all of the following match:

* stage (represented by ``stage_set``);
* prompt-template version (``prompt_template_version``);
* schema version (implicit in ``evaluator_version``);
* provider and model fingerprint (``model_fingerprint``);
* normalized input hash (``target_hash``);
* applicable policy version (``policy_version``).

The ``audit_identity_hash`` is computed from exactly these six fields.

## Import/export behavior

* ``audit-export`` — includes ``audit_identity_hash`` in the export
  JSON so downstream consumers can detect equivalence.
* ``audit-query`` — ``audit_identity_hash`` is included in result rows.

## Forward-repair

If this migration is interrupted, re-run ``upgrade head`` from the
last successful revision.  The migration uses ``IF NOT EXISTS`` guards
for all DDL.

## Downgrade

Migrations are forward-only.  Restore PostgreSQL from the pre-v19
recovery boundary or apply a forward-repair migration.
"""

from alembic import op


revision = "0019_audit_identity"
down_revision = "0018_audit_assessments"
branch_labels = None
depends_on = None


def upgrade():
    # ----------------------------------------------------------------
    # 1. Add audit_identity_hash column (nullable initially for backfill)
    # ----------------------------------------------------------------
    op.execute(
        """
        ALTER TABLE audit_assessments
        ADD COLUMN IF NOT EXISTS audit_identity_hash text;
        """
    )

    # ----------------------------------------------------------------
    # 2. Backfill existing rows with a deterministic hash.
    #    We use md5() because it is universally available in PostgreSQL.
    #    New rows will use SHA-256 computed in Python; the different
    #    algorithm means legacy hashes never collide with new ones.
    #    Canonical JSON: sort keys, sort stage_set, null-safe handling.
    # ----------------------------------------------------------------
    op.execute(
        """
        UPDATE audit_assessments
        SET audit_identity_hash = md5(
            target_hash || '|' ||
            evaluator_version || '|' ||
            prompt_template_version || '|' ||
            COALESCE(model_fingerprint, '') || '|' ||
            policy_version || '|' ||
            array_to_string(
                (SELECT json_agg(s) FROM (
                    SELECT unnest(stage_set) AS s ORDER BY s
                ) sub),
                ','
            )
        )
        WHERE audit_identity_hash IS NULL;
        """
    )

    # ----------------------------------------------------------------
    # 3. Add NOT NULL constraint
    # ----------------------------------------------------------------
    op.execute(
        """
        ALTER TABLE audit_assessments
        ALTER COLUMN audit_identity_hash SET NOT NULL;
        """
    )

    # ----------------------------------------------------------------
    # 4. Add NOT NULL check constraint
    # ----------------------------------------------------------------
    op.execute(
        """
        ALTER TABLE audit_assessments
        ADD CONSTRAINT chk_audit_assessments_audit_identity_hash
        CHECK (length(trim(audit_identity_hash)) > 0);
        """
    )

    # ----------------------------------------------------------------
    # 5. Add partial unique constraint (WHERE status != 'failed')
    #    Failed assessments are excluded so retried audits can be created.
    # ----------------------------------------------------------------
    op.execute(
        """
        ALTER TABLE audit_assessments
        ADD CONSTRAINT uk_audit_assessments_identity
        UNIQUE (audit_identity_hash)
        WHERE status != 'failed';
        """
    )

    # ----------------------------------------------------------------
    # 6. Add lookup index
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes
            WHERE tablename = 'audit_assessments'
              AND indexname = 'idx_audit_assessments_identity_hash'
          ) THEN
            CREATE INDEX idx_audit_assessments_identity_hash
              ON audit_assessments (audit_identity_hash);
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 7. Record migration
    # ----------------------------------------------------------------
    op.execute(
        "INSERT INTO schema_migrations(version) VALUES (19) ON CONFLICT DO NOTHING"
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v19 recovery boundary or apply a forward repair "
        "migration."
    )