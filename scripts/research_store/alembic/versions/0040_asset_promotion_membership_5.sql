        CREATE FUNCTION bind_index_checkpoint_asset_membership()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path=pg_catalog,public
        AS $function$
        DECLARE
          active_seal run_asset_membership_seals%ROWTYPE;
          sealed_chunk_ids uuid[];
        BEGIN
          SELECT * INTO active_seal FROM run_asset_membership_seals
           WHERE run_id=NEW.run_id AND status='sealed' FOR SHARE;
          IF NOT FOUND THEN
            RAISE EXCEPTION
              'index checkpoint requires sealed completion-critical membership'
              USING ERRCODE='55000';
          END IF;
          SELECT array_agg(DISTINCT chunk_id ORDER BY chunk_id)
            INTO sealed_chunk_ids
            FROM run_asset_membership_members member
            CROSS JOIN LATERAL unnest(member.chunk_ids) AS chunks(chunk_id)
           WHERE member.seal_id=active_seal.id;
          IF NEW.lifecycle_revision<>active_seal.lifecycle_revision THEN
            RAISE EXCEPTION
              'checkpoint lifecycle revision % does not match asset seal revision %',
              NEW.lifecycle_revision,active_seal.lifecycle_revision
              USING ERRCODE='23514';
          END IF;
          IF NEW.expected_count<>active_seal.expected_chunk_count THEN
            RAISE EXCEPTION
              'checkpoint chunk count % does not match sealed expected chunk count %',
              NEW.expected_count,active_seal.expected_chunk_count
              USING ERRCODE='23514';
          END IF;
          IF NEW.entity_ids IS DISTINCT FROM sealed_chunk_ids THEN
            RAISE EXCEPTION
              'checkpoint entity IDs do not equal sealed completion membership'
              USING ERRCODE='23514';
          END IF;
          NEW.asset_membership_seal_id := active_seal.id;
          NEW.asset_membership_sha256 := active_seal.membership_sha256;
          NEW.asset_expected_count := active_seal.expected_asset_count;
          NEW.asset_expected_chunk_count := active_seal.expected_chunk_count;
          RETURN NEW;
        END;
        $function$;
        CREATE TRIGGER indexing_checkpoint_binds_asset_membership_trigger
        BEFORE INSERT ON indexing_checkpoints
        FOR EACH ROW EXECUTE FUNCTION bind_index_checkpoint_asset_membership();
