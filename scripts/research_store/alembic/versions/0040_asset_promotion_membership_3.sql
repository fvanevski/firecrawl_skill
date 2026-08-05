        CREATE FUNCTION initialize_discovered_promotion_subject()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public
        AS $function$
        DECLARE
          current_lifecycle_revision bigint;
        BEGIN
          SELECT lifecycle_revision INTO current_lifecycle_revision
            FROM research_runs WHERE id=NEW.run_id FOR UPDATE;
          INSERT INTO run_asset_promotion_subjects(
            run_id,candidate_id,current_stage,stage_revision,provenance,
            actor_type,actor_identifier,policy_version,lifecycle_revision,
            reason_code,reason,created_at,updated_at
          ) VALUES(
            NEW.run_id,NEW.id,'discovered',0,'authoritative',
            'system','search-candidate-trigger','candidate-discovery-v1',
            current_lifecycle_revision,'candidate_persisted',
            'PostgreSQL search candidate was persisted',NEW.created_at,NEW.created_at
          ) ON CONFLICT DO NOTHING;
          RETURN NEW;
        END;
        $function$;
        CREATE TRIGGER search_candidate_initializes_promotion_subject_trigger
        AFTER INSERT ON search_candidates
        FOR EACH ROW EXECUTE FUNCTION initialize_discovered_promotion_subject();

        CREATE FUNCTION record_extraction_promotion_stages()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public
        AS $function$
        DECLARE
          subject_row run_asset_promotion_subjects%ROWTYPE;
          current_lifecycle_revision bigint;
          attempt_actor_identifier text;
          extraction_is_authoritative boolean;
        BEGIN
          SELECT lifecycle_revision INTO current_lifecycle_revision
            FROM research_runs WHERE id=NEW.run_id FOR UPDATE;
          attempt_actor_identifier := format('extraction-attempt:%s',NEW.id);
          extraction_is_authoritative :=
            NEW.exit_status::text='succeeded'
            AND NEW.end_time IS NOT NULL
            AND (
              NEW.raw_blob_sha256 IS NOT NULL
              OR NEW.normalized_blob_sha256 IS NOT NULL
            );

          SELECT * INTO subject_row FROM run_asset_promotion_subjects
           WHERE run_id=NEW.run_id AND candidate_id=NEW.candidate_id FOR UPDATE;
          IF NOT FOUND THEN
            INSERT INTO run_asset_promotion_subjects(
              run_id,candidate_id,current_stage,stage_revision,provenance,
              actor_type,actor_identifier,policy_version,lifecycle_revision,
              reason_code,reason
            ) VALUES(
              NEW.run_id,NEW.candidate_id,'discovered',0,'authoritative',
              'system',attempt_actor_identifier,'candidate-discovery-v1',
              current_lifecycle_revision,'subject_initialized_for_new_extraction',
              'A new extraction established the subject; no history was inferred'
            ) RETURNING * INTO subject_row;
          END IF;

          IF subject_row.current_stage='discovered' THEN
            UPDATE run_asset_promotion_subjects
               SET current_stage='selected_for_extraction',
                   actor_type='system',
                   actor_identifier=attempt_actor_identifier,
                   policy_version='extraction-attempt-v1',
                   lifecycle_revision=current_lifecycle_revision,
                   reason_code='extraction_attempt_started',
                   reason='Candidate received an authoritative extraction attempt'
             WHERE id=subject_row.id;
            SELECT * INTO subject_row FROM run_asset_promotion_subjects
             WHERE id=subject_row.id;
          END IF;

          IF extraction_is_authoritative
             AND subject_row.current_stage='selected_for_extraction' THEN
            UPDATE run_asset_promotion_subjects
               SET current_stage='extracted',
                   actor_type='system',
                   actor_identifier=attempt_actor_identifier,
                   policy_version='extraction-attempt-v1',
                   lifecycle_revision=current_lifecycle_revision,
                   reason_code='extraction_attempt_succeeded',
                   reason='A finalized extraction attempt persisted successful output bytes'
             WHERE id=subject_row.id;
          END IF;
          RETURN NEW;
        END;
        $function$;
        CREATE TRIGGER extraction_attempt_initializes_promotion_stage_trigger
        AFTER INSERT ON extraction_attempts
        FOR EACH ROW EXECUTE FUNCTION record_extraction_promotion_stages();
        CREATE TRIGGER extraction_attempt_finalizes_promotion_stage_trigger
        AFTER UPDATE OF exit_status,end_time,raw_blob_sha256,normalized_blob_sha256
        ON extraction_attempts
        FOR EACH ROW EXECUTE FUNCTION record_extraction_promotion_stages();

        CREATE FUNCTION guard_extraction_attempt_after_promotion()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public
        AS $function$
        BEGIN
          IF EXISTS (
            SELECT 1
              FROM run_asset_promotion_events event
             WHERE event.run_id=OLD.run_id
               AND event.to_stage='extracted'
               AND event.actor_identifier=format('extraction-attempt:%s',OLD.id)
          ) AND (
            NEW.exit_status IS DISTINCT FROM OLD.exit_status
            OR NEW.end_time IS DISTINCT FROM OLD.end_time
            OR NEW.raw_blob_sha256 IS DISTINCT FROM OLD.raw_blob_sha256
            OR NEW.normalized_blob_sha256 IS DISTINCT FROM
               OLD.normalized_blob_sha256
          ) THEN
            RAISE EXCEPTION
              'finalized extraction attempt % already supports immutable promotion provenance',
              OLD.id
              USING ERRCODE='55000';
          END IF;
          RETURN NEW;
        END;
        $function$;
        CREATE TRIGGER extraction_attempt_promoted_provenance_guard_trigger
        BEFORE UPDATE OF exit_status,end_time,raw_blob_sha256,normalized_blob_sha256
        ON extraction_attempts
        FOR EACH ROW EXECUTE FUNCTION guard_extraction_attempt_after_promotion();

        CREATE FUNCTION retain_linked_run_asset_subject()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public
        AS $function$
        DECLARE
          resolved_candidate_id uuid;
          resolved_subject run_asset_promotion_subjects%ROWTYPE;
          current_lifecycle_revision bigint;
        BEGIN
          SELECT lifecycle_revision INTO current_lifecycle_revision
            FROM research_runs WHERE id=NEW.run_id FOR UPDATE;
          SELECT attempt.candidate_id INTO resolved_candidate_id
            FROM asset_snapshots snapshot
            JOIN extraction_attempts attempt
              ON attempt.id=snapshot.extraction_attempt_id
           WHERE snapshot.id=NEW.snapshot_id AND attempt.run_id=NEW.run_id;

          IF resolved_candidate_id IS NOT NULL THEN
            SELECT * INTO resolved_subject FROM run_asset_promotion_subjects
             WHERE run_id=NEW.run_id AND candidate_id=resolved_candidate_id
             FOR UPDATE;
          END IF;

          IF resolved_subject.id IS NULL THEN
            INSERT INTO run_asset_promotion_subjects(
              run_id,candidate_id,snapshot_id,role,current_stage,stage_revision,
              provenance,actor_type,actor_identifier,policy_version,
              lifecycle_revision,reason_code,reason
            ) VALUES(
              NEW.run_id,resolved_candidate_id,NEW.snapshot_id,NEW.role,
              'retained',0,'direct_retention','system',
              'research-run-asset-trigger','run-asset-retention-adapter-v1',
              current_lifecycle_revision,'snapshot_retained_without_stage_history',
              'Snapshot retention is authoritative; earlier stages were not inferred'
            );
            RETURN NEW;
          END IF;

          IF resolved_subject.snapshot_id IS NULL THEN
            UPDATE run_asset_promotion_subjects
               SET snapshot_id=NEW.snapshot_id,role=NEW.role
             WHERE id=resolved_subject.id;
          ELSIF resolved_subject.snapshot_id<>NEW.snapshot_id
             OR resolved_subject.role<>NEW.role THEN
            RAISE EXCEPTION 'candidate promotion subject is linked to another run asset'
              USING ERRCODE='23514';
          END IF;

          IF resolved_subject.current_stage='extracted' THEN
            UPDATE run_asset_promotion_subjects
               SET current_stage='retained',
                   actor_type='system',
                   actor_identifier='research-run-asset-trigger',
                   policy_version='run-asset-retention-v1',
                   lifecycle_revision=current_lifecycle_revision,
                   reason_code='snapshot_retained_for_run',
                   reason='Successful extraction was retained as a PostgreSQL run asset'
             WHERE id=resolved_subject.id;
          ELSIF resolved_subject.current_stage NOT IN (
            'retained','evidence_eligible','completion_critical','rejected'
          ) THEN
            RAISE EXCEPTION 'run asset cannot be retained from promotion stage %',
              resolved_subject.current_stage
              USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END;
        $function$;
        CREATE TRIGGER research_run_asset_retains_promotion_subject_trigger
        AFTER INSERT ON research_run_assets
        FOR EACH ROW EXECUTE FUNCTION retain_linked_run_asset_subject();
