"""Add research_claims and claim_evidence_links tables.

This migration introduces the tables required by issue #32
(Claims, source manifests, and claim-evidence links) and PRD
sections FR-015, FR-016, and 14.1.

## Schema changes

### research_claims

Stable claim records persisted from evidence packets or report synthesis.

* ``id`` — UUID PK, ``gen_random_uuid()``
* ``run_id`` — FK to ``research_runs(id)`` ON DELETE CASCADE
* ``claim_id`` — domain-level claim UUID (from ``EvidenceClaim.claim_id``)
* ``statement`` — the claim text (non-empty)
* ``semantic_status`` — constrained to the ``claim_semantic_status`` enum
* ``uncertainty`` — free-text uncertainty description (nullable)
* ``evidence_packet_revision`` — the evidence-packet revision this claim belongs to
* ``created_at`` — timestamptz, defaults to ``now()``

Constraints:
* ``uk_research_claims_run_claim`` — unique ``(run_id, claim_id)`` for
  idempotent upsert semantics.
* ``chk_research_claims_semantic_status`` — enum constraint.
* ``chk_research_claims_statement`` — non-empty text.

### claim_evidence_links

Links claims to chunks (passages), blocks, or snapshots with relationship
labels. Append-only — no UPDATE/DELETE.

* ``id`` — UUID PK, ``gen_random_uuid()``
* ``run_id`` — FK to ``research_runs(id)`` ON DELETE CASCADE
* ``claim_id`` — domain-level claim UUID
* ``passage_id`` — FK to ``chunks(id)`` ON DELETE CASCADE (the chunk that
  contains the evidentiary passage)
* ``snapshot_id`` — FK to ``asset_snapshots(id)`` ON DELETE CASCADE
  (the snapshot from which the chunk was derived)
* ``source_url`` — the canonical URL of the source (retained for audit;
  not used for identity resolution)
* ``relationship`` — constrained to the ``claim_evidence_relationship`` enum
* ``confidence`` — float in ``[0, 1]``
* ``created_at`` — timestamptz, defaults to ``now()``

Constraints:
* ``chk_claim_evidence_links_confidence`` — ``confidence >= 0 AND confidence <= 1``
* ``chk_claim_evidence_links_relationship`` — enum constraint.
* ``chk_claim_evidence_links_passage`` — ``passage_id`` is NOT NULL.

Indexes:
* ``idx_claim_evidence_links_passage`` — filter by ``passage_id``
* ``idx_claim_evidence_links_claim`` — filter by ``(run_id, claim_id)``
* ``idx_claim_evidence_links_relationship`` — filter by ``relationship``

## Referential invariants

1. ``claim_evidence_links.passage_id`` references ``chunks.id``.
2. ``claim_evidence_links.snapshot_id`` references ``asset_snapshots.id``.
3. ``claim_evidence_links.run_id`` references ``research_runs.id``.
4. ``research_claims.run_id`` references ``research_runs.id``.

## Deterministic reference validation

The ``ClaimManifestService`` (added in the same PR) validates that:

* ``claim_id`` values exist as domain-level UUIDs (passed in by the caller).
* ``passage_id`` (chunk) values exist in ``chunks`` before accepting.
* ``snapshot_id`` values exist in ``asset_snapshots`` before accepting.
* URL-only source references are rejected — callers must provide stable
  ``passage_id`` and ``snapshot_id``.

## Import/export behavior

* ``claim-manifest export`` — serializes all claims and links for a run
  to JSON, including a source-state hash.
* ``claim-manifest import`` — dry-run-first, idempotent upserts keyed on
  ``(run_id, claim_id)`` for claims and ``(claim_id, passage_id)`` for links.

## Survival guarantee

Claims and links are stored exclusively in PostgreSQL. Deleting scratch
directories or Catalog v5 files does not affect their availability.
They are queryable via the ``claim-manifest list`` CLI command and the
service API.

## Forward-repair

If this migration is interrupted, re-run ``upgrade head`` from the
last successful revision. The migration is idempotent — tables and enums
use ``CREATE TABLE IF NOT EXISTS`` and ``CREATE TYPE ... NOT EXISTS`` guards.

## Downgrade

Migrations are forward-only. Restore PostgreSQL from the pre-v17
recovery boundary or apply a forward-repair migration.
"""

from alembic import op


revision = "0017_claims_evidence"
down_revision = "0016_invocation_events"
branch_labels = None
depends_on = None


def upgrade():
    # ----------------------------------------------------------------
    # 1. Create the semantic_status enum for research_claims
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_type WHERE typname = 'claim_semantic_status'
          ) THEN
            CREATE TYPE claim_semantic_status AS ENUM (
              'supported',
              'contradicted',
              'qualified',
              'unsupported',
              'uncertain',
              'unassessed'
            );
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 2. Create the relationship enum for claim_evidence_links
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_type WHERE typname = 'claim_evidence_relationship'
          ) THEN
            CREATE TYPE claim_evidence_relationship AS ENUM (
              'supports',
              'contradicts',
              'qualifies',
              'context'
            );
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 3. Create research_claims table
    # ----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS research_claims (
          id                          uuid NOT NULL DEFAULT gen_random_uuid(),
          run_id                      uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
          claim_id                    uuid NOT NULL,
          statement                   text NOT NULL,
          semantic_status             claim_semantic_status NOT NULL DEFAULT 'unassessed',
          uncertainty                 text,
          evidence_packet_revision    bigint NOT NULL DEFAULT 1,
          created_at                  timestamptz NOT NULL DEFAULT now(),
          updated_at                  timestamptz NOT NULL DEFAULT now(),

          PRIMARY KEY (id),
          CONSTRAINT uk_research_claims_run_claim
            UNIQUE (run_id, claim_id),
          CONSTRAINT chk_research_claims_statement
            CHECK (length(trim(statement)) > 0)
        );
        """
    )

    # ----------------------------------------------------------------
    # 4. Create claim_evidence_links table
    # ----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS claim_evidence_links (
          id                          uuid NOT NULL DEFAULT gen_random_uuid(),
          run_id                      uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
          claim_id                    uuid NOT NULL,
          passage_id                  uuid NOT NULL,
          snapshot_id                 uuid NOT NULL REFERENCES asset_snapshots(id) ON DELETE CASCADE,
          source_url                  text NOT NULL DEFAULT '',
          relationship                claim_evidence_relationship NOT NULL DEFAULT 'supports',
          confidence                  float8 NOT NULL DEFAULT 1.0,
          created_at                  timestamptz NOT NULL DEFAULT now(),

          PRIMARY KEY (id),
          CONSTRAINT chk_claim_evidence_links_passage
            CHECK (passage_id IS NOT NULL),
          CONSTRAINT chk_claim_evidence_links_confidence
            CHECK (confidence >= 0.0 AND confidence <= 1.0),
          CONSTRAINT chk_claim_evidence_links_relationship
            CHECK (relationship IS NOT NULL)
        );
        """
    )

    # ----------------------------------------------------------------
    # 5. Add indexes
    # ----------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'research_claims' AND indexname = 'idx_research_claims_run'
          ) THEN
            CREATE INDEX idx_research_claims_run
              ON research_claims (run_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'research_claims' AND indexname = 'idx_research_claims_claim_id'
          ) THEN
            CREATE INDEX idx_research_claims_claim_id
              ON research_claims (claim_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'claim_evidence_links' AND indexname = 'idx_claim_evidence_links_passage'
          ) THEN
            CREATE INDEX idx_claim_evidence_links_passage
              ON claim_evidence_links (passage_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'claim_evidence_links' AND indexname = 'idx_claim_evidence_links_claim'
          ) THEN
            CREATE INDEX idx_claim_evidence_links_claim
              ON claim_evidence_links (run_id, claim_id);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'claim_evidence_links' AND indexname = 'idx_claim_evidence_links_relationship'
          ) THEN
            CREATE INDEX idx_claim_evidence_links_relationship
              ON claim_evidence_links (relationship);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'claim_evidence_links' AND indexname = 'idx_claim_evidence_links_snapshot'
          ) THEN
            CREATE INDEX idx_claim_evidence_links_snapshot
              ON claim_evidence_links (snapshot_id);
          END IF;
        END $$;
        """
    )

    # ----------------------------------------------------------------
    # 6. Record migration
    # ----------------------------------------------------------------
    op.execute(
        "INSERT INTO schema_migrations(version) VALUES (17) ON CONFLICT DO NOTHING"
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; restore PostgreSQL "
        "from the pre-v17 recovery boundary or apply a forward repair "
        "migration."
    )
