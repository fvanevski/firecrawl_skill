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

        CREATE FUNCTION canonical_asset_membership_member_payload(
          subject_id uuid,
          snapshot_id uuid,
          role text,
          chunk_ids uuid[]
        ) RETURNS text
        LANGUAGE sql
        IMMUTABLE
        STRICT
        SET search_path=pg_catalog
        AS $function$
          SELECT '{"chunk_ids":['
            || COALESCE((
              SELECT string_agg(to_json(chunk_id::text)::text,',' ORDER BY ordinal)
                FROM unnest(chunk_ids) WITH ORDINALITY AS chunk(chunk_id,ordinal)
            ),'')
            || '],"role":' || to_json(role)::text
            || ',"snapshot_id":' || to_json(snapshot_id::text)::text
            || ',"subject_id":' || to_json(subject_id::text)::text
            || '}'
        $function$;

        CREATE FUNCTION validate_run_asset_membership_seal(target_seal_id uuid)
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public
        AS $function$
        DECLARE
          seal_row run_asset_membership_seals%ROWTYPE;
          member_count bigint;
          ordinal_count bigint;
          minimum_ordinal integer;
          maximum_ordinal integer;
          distinct_chunk_count bigint;
          computed_membership_sha256 text;
        BEGIN
          SELECT * INTO seal_row
            FROM run_asset_membership_seals WHERE id=target_seal_id;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'asset membership seal % does not exist',target_seal_id
              USING ERRCODE='23503';
          END IF;

          IF EXISTS (
            SELECT 1
              FROM run_asset_membership_members member
             WHERE member.seal_id=target_seal_id
               AND (
                 array_position(member.chunk_ids,NULL) IS NOT NULL
                 OR cardinality(member.chunk_ids)<>(
                   SELECT count(DISTINCT chunk_id)
                     FROM unnest(member.chunk_ids) AS chunk(chunk_id)
                 )
                 OR member.chunk_ids IS DISTINCT FROM ARRAY(
                   SELECT chunk_id
                     FROM unnest(member.chunk_ids) AS chunk(chunk_id)
                    ORDER BY chunk_id
                 )
               )
          ) THEN
            RAISE EXCEPTION
              'asset membership member chunk IDs must be non-null, unique, and sorted'
              USING ERRCODE='23514';
          END IF;

          IF EXISTS (
            SELECT 1
              FROM run_asset_membership_members member
             WHERE member.seal_id=target_seal_id
               AND member.member_sha256<>encode(digest(
                 canonical_asset_membership_member_payload(
                   member.subject_id,member.snapshot_id,member.role,member.chunk_ids
                 ),'sha256'),'hex')
          ) THEN
            RAISE EXCEPTION
              'asset membership member SHA-256 does not address its persisted payload'
              USING ERRCODE='23514';
          END IF;

          SELECT count(*),count(DISTINCT ordinal),min(ordinal),max(ordinal),
                 encode(digest(
                   '[' || string_agg(
                     canonical_asset_membership_member_payload(
                       member.subject_id,member.snapshot_id,member.role,
                       member.chunk_ids
                     ),',' ORDER BY member.ordinal
                   ) || ']',
                   'sha256'
                 ),'hex')
            INTO member_count,ordinal_count,minimum_ordinal,maximum_ordinal,
                 computed_membership_sha256
            FROM run_asset_membership_members member
           WHERE member.seal_id=target_seal_id;

          SELECT count(DISTINCT chunk_id)
            INTO distinct_chunk_count
            FROM run_asset_membership_members member
            CROSS JOIN LATERAL unnest(member.chunk_ids) AS chunk(chunk_id)
           WHERE member.seal_id=target_seal_id;

          IF member_count=0
             OR ordinal_count<>member_count
             OR minimum_ordinal<>0
             OR maximum_ordinal<>member_count-1 THEN
            RAISE EXCEPTION
              'asset membership seal requires a non-empty contiguous member ordering'
              USING ERRCODE='23514';
          END IF;
          IF seal_row.expected_asset_count<>member_count THEN
            RAISE EXCEPTION
              'asset membership expected asset count % does not equal persisted count %',
              seal_row.expected_asset_count,member_count
              USING ERRCODE='23514';
          END IF;
          IF seal_row.expected_chunk_count<>distinct_chunk_count THEN
            RAISE EXCEPTION
              'asset membership expected chunk count % does not equal persisted count %',
              seal_row.expected_chunk_count,distinct_chunk_count
              USING ERRCODE='23514';
          END IF;
          IF seal_row.membership_sha256<>computed_membership_sha256 THEN
            RAISE EXCEPTION
              'asset membership seal SHA-256 does not address its persisted members'
              USING ERRCODE='23514';
          END IF;
        END;
        $function$;

        CREATE FUNCTION validate_run_asset_membership_seal_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public
        AS $function$
        BEGIN
          PERFORM validate_run_asset_membership_seal(
            CASE WHEN TG_TABLE_NAME='run_asset_membership_members'
                 THEN NEW.seal_id ELSE NEW.id END
          );
          RETURN NULL;
        END;
        $function$;
        CREATE CONSTRAINT TRIGGER run_asset_membership_seal_validate_trigger
        AFTER INSERT ON run_asset_membership_seals
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION validate_run_asset_membership_seal_trigger();
        CREATE CONSTRAINT TRIGGER run_asset_membership_member_validate_trigger
        AFTER INSERT ON run_asset_membership_members
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION validate_run_asset_membership_seal_trigger();

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
