"""Add target-scoped audit identity and completed-assessment idempotency.

Revision 0018 constrained assessments only by target hash. That prevented a
new assessment when evaluator, prompt, policy, model, or stage configuration
changed. This migration replaces that rule with a target-scoped identity for
completed assessments.

Partial and failed assessments are retained as historical attempts and are not
reused. A later completed retry can therefore coexist with them.

The Python backfill intentionally uses the same canonical JSON and SHA-256
implementation as research_store.service.compute_audit_identity_hash. Keeping
the helper local makes the migration immutable while tests lock the two
implementations together.
"""

from __future__ import annotations

from hashlib import sha256
import json

from alembic import op
import sqlalchemy as sa


revision = "0019_audit_identity"
down_revision = "0018_audit_assessments"
branch_labels = None
depends_on = None

_IDENTITY_VERSION = "audit-identity-v1"
_LEGACY_MODEL_VERSION = "legacy-audit-model-v1"


def _canonical_hash(
    *,
    target_hash,
    evaluator_version,
    prompt_template_version,
    policy_version,
    stage_set,
    model_fingerprint,
):
    identity = {
        "identity_version": _IDENTITY_VERSION,
        "evaluator_version": evaluator_version,
        "model_fingerprint": model_fingerprint,
        "policy_version": policy_version,
        "prompt_template_version": prompt_template_version,
        "stage_set": sorted(set(stage_set)),
        "target_hash": target_hash,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _legacy_model_fingerprint(row):
    identity = {
        "implementation_version": _LEGACY_MODEL_VERSION,
        "provider": row["provider"] or "unknown",
        "model": row["model"] or "unknown",
        "evaluator_version": row["evaluator_version"],
        "prompt_template_version": row["prompt_template_version"],
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def upgrade():
    bind = op.get_bind()

    op.execute(
        """
        ALTER TABLE audit_assessments
        ADD COLUMN IF NOT EXISTS audit_identity_hash text
        """
    )

    rows = list(
        bind.execute(
            sa.text(
                """
                SELECT id, target_hash, evaluator_version,
                       prompt_template_version, policy_version, stage_set,
                       model_fingerprint, provider, model
                FROM audit_assessments
                WHERE audit_identity_hash IS NULL
                   OR length(audit_identity_hash) <> 64
                   OR audit_identity_hash !~ '^[0-9a-f]{64}$'
                   OR model_fingerprint IS NULL
                   OR length(trim(model_fingerprint)) = 0
                ORDER BY id
                """
            )
        ).mappings()
    )

    for row in rows:
        fingerprint = row["model_fingerprint"] or _legacy_model_fingerprint(row)
        identity_hash = _canonical_hash(
            target_hash=row["target_hash"],
            evaluator_version=row["evaluator_version"],
            prompt_template_version=row["prompt_template_version"],
            policy_version=row["policy_version"],
            stage_set=list(row["stage_set"]),
            model_fingerprint=fingerprint,
        )
        bind.execute(
            sa.text(
                """
                UPDATE audit_assessments
                SET model_fingerprint = :model_fingerprint,
                    audit_identity_hash = :audit_identity_hash
                WHERE id = :id
                """
            ),
            {
                "id": row["id"],
                "model_fingerprint": fingerprint,
                "audit_identity_hash": identity_hash,
            },
        )

    op.execute(
        """
        ALTER TABLE audit_assessments
        DROP CONSTRAINT IF EXISTS uk_audit_assessments_target
        """
    )
    op.execute(
        """
        ALTER TABLE audit_assessments
        ALTER COLUMN model_fingerprint SET NOT NULL,
        ALTER COLUMN audit_identity_hash SET NOT NULL
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'chk_audit_assessments_model_fingerprint'
              AND conrelid = 'audit_assessments'::regclass
          ) THEN
            ALTER TABLE audit_assessments
            ADD CONSTRAINT chk_audit_assessments_model_fingerprint
            CHECK (length(trim(model_fingerprint)) > 0);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'chk_audit_assessments_audit_identity_hash'
              AND conrelid = 'audit_assessments'::regclass
          ) THEN
            ALTER TABLE audit_assessments
            ADD CONSTRAINT chk_audit_assessments_audit_identity_hash
            CHECK (
              length(audit_identity_hash) = 64
              AND audit_identity_hash ~ '^[0-9a-f]{64}$'
            );
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
          uk_audit_assessments_completed_identity
        ON audit_assessments (
          run_id, target_type, target_id, audit_identity_hash
        )
        WHERE status = 'completed'
        """
    )
    op.execute(
        """
        INSERT INTO schema_migrations(version)
        VALUES (19)
        ON CONFLICT DO NOTHING
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v19 recovery boundary or apply a forward repair migration."
    )

