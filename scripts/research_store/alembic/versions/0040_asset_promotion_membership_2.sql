        CREATE FUNCTION run_asset_promotion_subject_guard()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public
        AS $function$
        DECLARE
          current_lifecycle_revision bigint;
          crossing_completion_boundary boolean;
        BEGIN
          SELECT lifecycle_revision INTO current_lifecycle_revision
            FROM research_runs WHERE id=NEW.run_id FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'research run % does not exist',NEW.run_id
              USING ERRCODE='23503';
          END IF;
          IF TG_OP='INSERT' THEN
            IF NEW.lifecycle_revision <> current_lifecycle_revision THEN
              RAISE EXCEPTION
                'asset promotion lifecycle revision is stale: expected %, current %',
                NEW.lifecycle_revision,current_lifecycle_revision
                USING ERRCODE='40001';
            END IF;
            IF NEW.stage_revision <> 0 THEN
              RAISE EXCEPTION 'initial asset promotion revision must be zero'
                USING ERRCODE='23514';
            END IF;
            IF NEW.current_stage='discovered' THEN
              IF NEW.candidate_id IS NULL OR NEW.provenance<>'authoritative' THEN
                RAISE EXCEPTION
                  'discovered subjects require authoritative candidate identity'
                  USING ERRCODE='23514';
              END IF;
            ELSIF NEW.current_stage='retained' THEN
              IF NEW.snapshot_id IS NULL OR NEW.role IS NULL
                 OR NEW.provenance<>'direct_retention' THEN
                RAISE EXCEPTION
                  'direct retained subjects require snapshot, role, and provenance'
                  USING ERRCODE='23514';
              END IF;
            ELSE
              RAISE EXCEPTION 'initial stage must be discovered or retained'
                USING ERRCODE='23514';
            END IF;
            NEW.updated_at := COALESCE(NEW.updated_at,now());
            RETURN NEW;
          END IF;

          IF NEW.run_id IS DISTINCT FROM OLD.run_id
             OR NEW.candidate_id IS DISTINCT FROM OLD.candidate_id
             OR NEW.provenance IS DISTINCT FROM OLD.provenance THEN
            RAISE EXCEPTION 'asset promotion identity and provenance are immutable'
              USING ERRCODE='55000';
          END IF;
          IF OLD.snapshot_id IS NOT NULL
             AND NEW.snapshot_id IS DISTINCT FROM OLD.snapshot_id THEN
            RAISE EXCEPTION 'asset promotion snapshot identity is immutable'
              USING ERRCODE='55000';
          END IF;
          IF OLD.role IS NOT NULL AND NEW.role IS DISTINCT FROM OLD.role THEN
            RAISE EXCEPTION 'asset promotion role is immutable'
              USING ERRCODE='55000';
          END IF;

          IF NEW.current_stage IS NOT DISTINCT FROM OLD.current_stage THEN
            IF NEW.stage_revision IS DISTINCT FROM OLD.stage_revision
               OR NEW.actor_type IS DISTINCT FROM OLD.actor_type
               OR NEW.actor_identifier IS DISTINCT FROM OLD.actor_identifier
               OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
               OR NEW.lifecycle_revision IS DISTINCT FROM OLD.lifecycle_revision
               OR NEW.reason_code IS DISTINCT FROM OLD.reason_code
               OR NEW.reason IS DISTINCT FROM OLD.reason THEN
              RAISE EXCEPTION
                'promotion metadata may change only with the promotion stage'
                USING ERRCODE='55000';
            END IF;
            NEW.updated_at := OLD.updated_at;
            RETURN NEW;
          END IF;

          IF NEW.lifecycle_revision <> current_lifecycle_revision THEN
            RAISE EXCEPTION
              'asset promotion lifecycle revision is stale: expected %, current %',
              NEW.lifecycle_revision,current_lifecycle_revision
              USING ERRCODE='40001';
          END IF;
          IF NEW.stage_revision IS DISTINCT FROM OLD.stage_revision THEN
            RAISE EXCEPTION 'asset promotion revision is trigger-managed'
              USING ERRCODE='55000';
          END IF;
          IF NOT (
            (OLD.current_stage='discovered'
              AND NEW.current_stage IN ('selected_for_extraction','rejected'))
            OR (OLD.current_stage='selected_for_extraction'
              AND NEW.current_stage IN ('extracted','rejected'))
            OR (OLD.current_stage='extracted'
              AND NEW.current_stage IN ('retained','rejected'))
            OR (OLD.current_stage='retained'
              AND NEW.current_stage IN ('evidence_eligible','rejected'))
            OR (OLD.current_stage='evidence_eligible'
              AND NEW.current_stage IN ('completion_critical','rejected'))
            OR (OLD.current_stage='completion_critical'
              AND NEW.current_stage='rejected')
          ) THEN
            RAISE EXCEPTION 'invalid asset promotion transition % -> %',
              OLD.current_stage,NEW.current_stage
              USING ERRCODE='23514';
          END IF;

          crossing_completion_boundary :=
            OLD.current_stage='completion_critical'
            OR NEW.current_stage='completion_critical';
          IF crossing_completion_boundary AND EXISTS (
            SELECT 1 FROM run_asset_membership_seals
             WHERE run_id=NEW.run_id AND status='sealed'
          ) THEN
            RAISE EXCEPTION
              'completion membership is sealed; reopen it before changing membership'
              USING ERRCODE='55000';
          END IF;

          NEW.stage_revision := OLD.stage_revision + 1;
          NEW.updated_at := now();
          RETURN NEW;
        END;
        $function$;

        CREATE TRIGGER run_asset_promotion_subject_guard_trigger
        BEFORE INSERT OR UPDATE ON run_asset_promotion_subjects
        FOR EACH ROW EXECUTE FUNCTION run_asset_promotion_subject_guard();

        CREATE FUNCTION run_asset_promotion_event_ledger()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public
        AS $function$
        BEGIN
          IF TG_OP='INSERT' THEN
            INSERT INTO run_asset_promotion_events(
              subject_id,run_id,from_stage,to_stage,stage_revision,
              actor_type,actor_identifier,policy_version,lifecycle_revision,
              reason_code,reason,occurred_at
            ) VALUES(
              NEW.id,NEW.run_id,NULL,NEW.current_stage,NEW.stage_revision,
              NEW.actor_type,NEW.actor_identifier,NEW.policy_version,
              NEW.lifecycle_revision,NEW.reason_code,NEW.reason,NEW.updated_at
            );
          ELSIF NEW.current_stage IS DISTINCT FROM OLD.current_stage THEN
            INSERT INTO run_asset_promotion_events(
              subject_id,run_id,from_stage,to_stage,stage_revision,
              actor_type,actor_identifier,policy_version,lifecycle_revision,
              reason_code,reason,occurred_at
            ) VALUES(
              NEW.id,NEW.run_id,OLD.current_stage,NEW.current_stage,
              NEW.stage_revision,NEW.actor_type,NEW.actor_identifier,
              NEW.policy_version,NEW.lifecycle_revision,NEW.reason_code,
              NEW.reason,NEW.updated_at
            );
          END IF;
          RETURN NEW;
        END;
        $function$;

        CREATE TRIGGER run_asset_promotion_event_ledger_trigger
        AFTER INSERT OR UPDATE OF current_stage ON run_asset_promotion_subjects
        FOR EACH ROW EXECUTE FUNCTION run_asset_promotion_event_ledger();

        CREATE FUNCTION reject_append_only_promotion_event_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
          RAISE EXCEPTION 'run_asset_promotion_events is append-only'
            USING ERRCODE='55000';
        END;
        $function$;
        CREATE TRIGGER run_asset_promotion_events_append_only_trigger
        BEFORE UPDATE OR DELETE ON run_asset_promotion_events
        FOR EACH ROW EXECUTE FUNCTION reject_append_only_promotion_event_change();
