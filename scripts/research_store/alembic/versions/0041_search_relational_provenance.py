"""Add relational search-attempt provenance and terminal plan-query states.

Historical rows are backfilled only when the existing transport metadata proves a
same-run invocation and a positive provider-attempt ordinal.  Ambiguous or
incomplete history remains explicitly unresolved.
"""

from alembic import op

revision = "0041_search_provenance"
down_revision = "0040_asset_promotion_membership"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        r"""
        ALTER TABLE search_responses
          ADD COLUMN invocation_id uuid,
          ADD COLUMN attempt_ordinal integer,
          ADD COLUMN provenance_status text;

        -- Existing rows begin unresolved.  The CTE below promotes only
        -- uniquely proved relationships; it never matches on query text,
        -- timestamps, ordering, or other fuzzy signals.
        UPDATE search_responses
           SET provenance_status = CASE
             WHEN backend = 'orchestrator' THEN 'not_applicable'
             ELSE 'historical_unresolved'
           END;

        WITH parsed_metadata AS (
          SELECT
            sr.id,
            sr.run_id,
            sr.backend,
            CASE
              WHEN sr.transport_metadata->>'invocation_id'
                     ~* '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
              THEN (sr.transport_metadata->>'invocation_id')::uuid
              ELSE NULL
            END AS invocation_id,
            CASE
              WHEN COALESCE(
                     NULLIF(sr.transport_metadata->>'attempt',''),
                     NULLIF(sr.transport_metadata->>'attempts','')
                   ) ~ '^[1-9][0-9]*$'
              THEN COALESCE(
                     NULLIF(sr.transport_metadata->>'attempt',''),
                     NULLIF(sr.transport_metadata->>'attempts','')
                   )::integer
              ELSE NULL
            END AS attempt_ordinal
          FROM search_responses sr
          WHERE sr.backend <> 'orchestrator'
        ),
        metadata_candidates AS (
          SELECT pm.*
          FROM parsed_metadata pm
          JOIN research_invocations ri
            ON ri.run_id = pm.run_id
           AND ri.id = pm.invocation_id
          WHERE pm.invocation_id IS NOT NULL
            AND pm.attempt_ordinal IS NOT NULL
        ),
        uniquely_proved AS (
          SELECT mc.*,
                 count(*) OVER (
                   PARTITION BY invocation_id, backend, attempt_ordinal
                 ) AS tuple_count
          FROM metadata_candidates mc
        )
        UPDATE search_responses sr
           SET invocation_id = up.invocation_id,
               attempt_ordinal = up.attempt_ordinal,
               provenance_status = 'resolved'
          FROM uniquely_proved up
         WHERE sr.id = up.id
           AND up.tuple_count = 1;

        ALTER TABLE search_responses
          ALTER COLUMN provenance_status SET DEFAULT 'unresolved_compatibility',
          ALTER COLUMN provenance_status SET NOT NULL,
          ADD CONSTRAINT search_responses_provenance_status_check
            CHECK(provenance_status IN (
              'resolved',
              'historical_unresolved',
              'unresolved_compatibility',
              'not_applicable'
            )),
          ADD CONSTRAINT search_responses_attempt_ordinal_check
            CHECK(attempt_ordinal IS NULL OR attempt_ordinal > 0),
          ADD CONSTRAINT search_responses_resolved_provenance_check
            CHECK(
              provenance_status <> 'resolved'
              OR (invocation_id IS NOT NULL AND attempt_ordinal IS NOT NULL)
            ),
          ADD CONSTRAINT search_responses_invocation_run_fk
            FOREIGN KEY(invocation_id,run_id)
            REFERENCES research_invocations(id,run_id);

        CREATE UNIQUE INDEX search_responses_invocation_attempt_uidx
          ON search_responses(
            invocation_id,
            backend,
            COALESCE(
              plan_query_id,
              '00000000-0000-0000-0000-000000000000'::uuid
            ),
            attempt_ordinal
          )
          WHERE provenance_status = 'resolved';

        CREATE INDEX search_responses_invocation_idx
          ON search_responses(invocation_id,created_at,id);

        ALTER TABLE search_plan_queries
          DROP CONSTRAINT search_plan_queries_status_check;

        -- Preserve any historical 'executed' rows as a read-only compatibility
        -- state.  New application transitions use the explicit terminal states.
        ALTER TABLE search_plan_queries
          ADD CONSTRAINT search_plan_queries_status_check
            CHECK(status IN (
              'pending','running','succeeded','empty','failed','cancelled','executed'
            ));

        CREATE FUNCTION reject_new_legacy_plan_query_status() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.status = 'executed'
             AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'executed') THEN
            RAISE EXCEPTION
              'search_plan_queries.status=executed is legacy read-only; '
              'use an explicit terminal state'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END $$;

        CREATE TRIGGER search_plan_queries_reject_new_executed
          BEFORE INSERT OR UPDATE OF status ON search_plan_queries
          FOR EACH ROW EXECUTE FUNCTION reject_new_legacy_plan_query_status();
        """
    )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; apply a forward repair "
        "for search provenance or restore PostgreSQL from backup"
    )
