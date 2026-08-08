"""Freeze authoritative completion provenance while a research run is terminal."""

from alembic import op

revision = "0044_terminal_provenance_guard"
down_revision = "0043_ingestion_batch_semantics"
branch_labels = None
depends_on = None

_PROVENANCE_TABLES = (
    "evidence_packets",
    "research_claims",
    "claim_evidence_links",
    "synthesis_stages",
    "semantic_calls",
    "semantic_artifacts",
    "run_asset_membership_seals",
    "run_asset_membership_members",
)


def upgrade():
    op.execute(
        """
        CREATE FUNCTION guard_nonterminal_completion_provenance_write()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public
        AS $function$
        DECLARE
          target_run_id uuid;
          run_state text;
        BEGIN
          IF TG_OP='DELETE' THEN
            target_run_id := OLD.run_id;
          ELSE
            target_run_id := NEW.run_id;
          END IF;

          IF TG_OP='UPDATE' AND OLD.run_id IS DISTINCT FROM NEW.run_id THEN
            RAISE EXCEPTION
              'completion provenance rows cannot move between research runs'
              USING ERRCODE='23514';
          END IF;

          SELECT state INTO run_state
            FROM public.research_runs
           WHERE id=target_run_id
           FOR KEY SHARE;

          IF NOT FOUND THEN
            -- A parent research_runs DELETE may cascade into guarded children
            -- after the parent row is no longer visible to this transaction.
            -- Preserve that existing whole-run cleanup behavior while still
            -- rejecting orphaning INSERT/UPDATE attempts.
            IF TG_OP='DELETE' THEN
              RETURN OLD;
            END IF;
            RAISE EXCEPTION
              'completion provenance references unknown research run %',
              target_run_id
              USING ERRCODE='23503';
          END IF;

          IF run_state IN ('completed','partial','failed','cancelled') THEN
            RAISE EXCEPTION
              'terminal research run provenance is immutable; reopen run % before mutating %',
              target_run_id,TG_TABLE_NAME
              USING ERRCODE='55000';
          END IF;

          IF TG_OP='DELETE' THEN
            RETURN OLD;
          END IF;
          RETURN NEW;
        END;
        $function$;
        """
    )
    for table in _PROVENANCE_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table}_terminal_provenance_guard
            BEFORE INSERT OR UPDATE OR DELETE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION guard_nonterminal_completion_provenance_write()
            """
        )


def downgrade():
    raise RuntimeError(
        "Research workflow migrations are forward-only; apply a terminal-provenance "
        "forward repair or restore PostgreSQL from backup"
    )
