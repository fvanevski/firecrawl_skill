        CREATE FUNCTION guard_sealed_run_asset_change()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public
        AS $function$
        DECLARE
          target_run_id uuid;
          target_snapshot_id uuid;
          target_role text;
        BEGIN
          target_run_id := CASE WHEN TG_OP='DELETE' THEN OLD.run_id ELSE NEW.run_id END;
          target_snapshot_id := CASE
            WHEN TG_OP='DELETE' THEN OLD.snapshot_id ELSE NEW.snapshot_id END;
          target_role := CASE WHEN TG_OP='DELETE' THEN OLD.role ELSE NEW.role END;
          PERFORM 1 FROM research_runs WHERE id=target_run_id FOR UPDATE;
          IF EXISTS (
            SELECT 1
              FROM run_asset_membership_seals seal
              JOIN run_asset_membership_members member ON member.seal_id=seal.id
             WHERE seal.run_id=target_run_id AND seal.status='sealed'
               AND member.snapshot_id=target_snapshot_id
               AND member.role=target_role
          ) THEN
            RAISE EXCEPTION
              'completion membership is sealed; reopen it before changing a member'
              USING ERRCODE='55000';
          END IF;
          IF TG_OP='DELETE' THEN
            RETURN OLD;
          END IF;
          RETURN NEW;
        END;
        $function$;
        CREATE TRIGGER research_run_asset_sealed_member_guard_trigger
        BEFORE DELETE OR UPDATE OF run_id,snapshot_id,role ON research_run_assets
        FOR EACH ROW EXECUTE FUNCTION guard_sealed_run_asset_change();

        CREATE FUNCTION guard_membership_seal_update()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public
        AS $function$
        DECLARE
          current_lifecycle_revision bigint;
        BEGIN
          IF TG_OP='DELETE' THEN
            RAISE EXCEPTION 'membership seals are append-only'
              USING ERRCODE='55000';
          END IF;
          IF OLD.status<>'sealed' OR NEW.status<>'reopened' THEN
            RAISE EXCEPTION 'membership seals are immutable except for explicit reopen'
              USING ERRCODE='55000';
          END IF;
          SELECT lifecycle_revision INTO current_lifecycle_revision
            FROM research_runs WHERE id=OLD.run_id FOR UPDATE;
          IF NEW.reopened_lifecycle_revision<>current_lifecycle_revision THEN
            RAISE EXCEPTION
              'membership reopen lifecycle revision is stale: expected %, current %',
              NEW.reopened_lifecycle_revision,current_lifecycle_revision
              USING ERRCODE='40001';
          END IF;
          IF NEW.id IS DISTINCT FROM OLD.id
             OR NEW.run_id IS DISTINCT FROM OLD.run_id
             OR NEW.seal_revision IS DISTINCT FROM OLD.seal_revision
             OR NEW.lifecycle_revision IS DISTINCT FROM OLD.lifecycle_revision
             OR NEW.membership_sha256 IS DISTINCT FROM OLD.membership_sha256
             OR NEW.expected_asset_count IS DISTINCT FROM OLD.expected_asset_count
             OR NEW.expected_chunk_count IS DISTINCT FROM OLD.expected_chunk_count
             OR NEW.actor_type IS DISTINCT FROM OLD.actor_type
             OR NEW.actor_identifier IS DISTINCT FROM OLD.actor_identifier
             OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
             OR NEW.reason_code IS DISTINCT FROM OLD.reason_code
             OR NEW.reason IS DISTINCT FROM OLD.reason
             OR NEW.sealed_at IS DISTINCT FROM OLD.sealed_at
             OR NEW.sealed_transaction_id IS DISTINCT FROM
                OLD.sealed_transaction_id THEN
            RAISE EXCEPTION 'sealed membership identity is immutable'
              USING ERRCODE='55000';
          END IF;
          UPDATE indexing_checkpoints
             SET status='invalidated',
                 invalidation_reason='asset_membership_reopened',
                 invalidated_at=now(),updated_at=now()
           WHERE run_id=OLD.run_id AND status='active';
          RETURN NEW;
        END;
        $function$;
        CREATE TRIGGER run_asset_membership_seal_update_guard_trigger
        BEFORE UPDATE OR DELETE ON run_asset_membership_seals
        FOR EACH ROW EXECUTE FUNCTION guard_membership_seal_update();

        CREATE FUNCTION reject_membership_member_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
          RAISE EXCEPTION 'run_asset_membership_members is append-only'
            USING ERRCODE='55000';
        END;
        $function$;
        CREATE TRIGGER run_asset_membership_members_append_only_trigger
        BEFORE UPDATE OR DELETE ON run_asset_membership_members
        FOR EACH ROW EXECUTE FUNCTION reject_membership_member_change();
