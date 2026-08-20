"""Add evidence_packets table for deterministic evidence persistence.

This migration introduces the table required by issue #54.

## Schema changes

### evidence_packets

Stores immutable, typed evidence packet revisions.

* ``id`` — UUID PK, ``gen_random_uuid()``
* ``run_id`` — FK to ``research_runs(id)`` ON DELETE CASCADE
* ``research_spec_id`` — domain-level spec UUID
* ``coverage_revision`` — the coverage revision that prompted this packet
* ``packet_revision`` — deterministic sequential revision number
* ``payload`` — JSONB blob containing the serialized EvidencePacket model
* ``created_at`` — timestamptz, defaults to ``now()``

Constraints:
* ``uk_evidence_packets_revision`` — unique ``(run_id, packet_revision)`` ensures immutability.
"""

from alembic import op

revision = "0029_evidence_packets"
down_revision = "0028_retrieval_trace"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS evidence_packets (
          id                          uuid NOT NULL DEFAULT gen_random_uuid(),
          run_id                      uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
          research_spec_id            uuid NOT NULL,
          coverage_revision           bigint NOT NULL,
          packet_revision             bigint NOT NULL,
          payload                     jsonb NOT NULL,
          created_at                  timestamptz NOT NULL DEFAULT now(),

          PRIMARY KEY (id),
          CONSTRAINT uk_evidence_packets_revision
            UNIQUE (run_id, packet_revision)
        );
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'evidence_packets' AND indexname = 'idx_evidence_packets_run'
          ) THEN
            CREATE INDEX idx_evidence_packets_run
              ON evidence_packets (run_id);
          END IF;
        END $$;
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v29 recovery boundary or apply a forward repair "
        "migration."
    )
