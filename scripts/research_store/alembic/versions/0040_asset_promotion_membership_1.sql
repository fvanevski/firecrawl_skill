        CREATE TABLE run_asset_promotion_subjects(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
          candidate_id uuid,
          snapshot_id uuid REFERENCES asset_snapshots(id) ON DELETE RESTRICT,
          role text,
          current_stage text NOT NULL CHECK(current_stage IN (
            'discovered','selected_for_extraction','extracted','retained',
            'evidence_eligible','completion_critical','rejected'
          )),
          stage_revision bigint NOT NULL DEFAULT 0 CHECK(stage_revision >= 0),
          provenance text NOT NULL CHECK(provenance IN (
            'authoritative','direct_retention'
          )),
          actor_type text NOT NULL CHECK(length(btrim(actor_type)) > 0),
          actor_identifier text,
          policy_version text NOT NULL CHECK(length(btrim(policy_version)) > 0),
          lifecycle_revision bigint NOT NULL CHECK(lifecycle_revision >= 0),
          reason_code text NOT NULL CHECK(length(btrim(reason_code)) > 0),
          reason text,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(id,run_id),
          UNIQUE(id,snapshot_id,role),
          FOREIGN KEY(candidate_id,run_id)
            REFERENCES search_candidates(id,run_id) ON DELETE CASCADE,
          CHECK(candidate_id IS NOT NULL OR snapshot_id IS NOT NULL),
          CHECK(
            (snapshot_id IS NULL AND role IS NULL)
            OR (snapshot_id IS NOT NULL AND role IS NOT NULL)
          )
        );
        CREATE UNIQUE INDEX run_asset_promotion_subject_candidate_uk
          ON run_asset_promotion_subjects(run_id,candidate_id)
          WHERE candidate_id IS NOT NULL;
        CREATE UNIQUE INDEX run_asset_promotion_subject_snapshot_uk
          ON run_asset_promotion_subjects(run_id,snapshot_id,role)
          WHERE snapshot_id IS NOT NULL;
        CREATE INDEX run_asset_promotion_subject_stage_idx
          ON run_asset_promotion_subjects(run_id,current_stage,id);

        CREATE TABLE run_asset_promotion_events(
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          subject_id uuid NOT NULL,
          run_id uuid NOT NULL,
          from_stage text,
          to_stage text NOT NULL CHECK(to_stage IN (
            'discovered','selected_for_extraction','extracted','retained',
            'evidence_eligible','completion_critical','rejected'
          )),
          stage_revision bigint NOT NULL CHECK(stage_revision >= 0),
          actor_type text NOT NULL CHECK(length(btrim(actor_type)) > 0),
          actor_identifier text,
          policy_version text NOT NULL CHECK(length(btrim(policy_version)) > 0),
          lifecycle_revision bigint NOT NULL CHECK(lifecycle_revision >= 0),
          reason_code text NOT NULL CHECK(length(btrim(reason_code)) > 0),
          reason text,
          occurred_at timestamptz NOT NULL DEFAULT now(),
          transaction_id xid8 NOT NULL DEFAULT pg_current_xact_id(),
          FOREIGN KEY(subject_id,run_id)
            REFERENCES run_asset_promotion_subjects(id,run_id)
            ON DELETE RESTRICT,
          UNIQUE(subject_id,stage_revision),
          CHECK(from_stage IS NULL OR from_stage IN (
            'discovered','selected_for_extraction','extracted','retained',
            'evidence_eligible','completion_critical','rejected'
          ))
        );
        CREATE INDEX run_asset_promotion_events_run_idx
          ON run_asset_promotion_events(run_id,occurred_at,id);

        CREATE TABLE run_asset_membership_seals(
          id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          run_id uuid NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
          seal_revision bigint NOT NULL CHECK(seal_revision >= 1),
          lifecycle_revision bigint NOT NULL CHECK(lifecycle_revision >= 0),
          status text NOT NULL DEFAULT 'sealed'
            CHECK(status IN ('sealed','reopened')),
          membership_sha256 text NOT NULL
            CHECK(membership_sha256 ~ '^[0-9a-f]{64}$'),
          expected_asset_count integer NOT NULL CHECK(expected_asset_count > 0),
          expected_chunk_count bigint NOT NULL CHECK(expected_chunk_count > 0),
          actor_type text NOT NULL CHECK(length(btrim(actor_type)) > 0),
          actor_identifier text,
          policy_version text NOT NULL CHECK(length(btrim(policy_version)) > 0),
          reason_code text NOT NULL CHECK(length(btrim(reason_code)) > 0),
          reason text,
          sealed_at timestamptz NOT NULL DEFAULT now(),
          sealed_transaction_id xid8 NOT NULL DEFAULT pg_current_xact_id(),
          reopened_at timestamptz,
          reopened_lifecycle_revision bigint,
          reopened_actor_type text,
          reopened_actor_identifier text,
          reopened_policy_version text,
          reopened_reason_code text,
          reopened_reason text,
          reopened_transaction_id xid8,
          UNIQUE(run_id,seal_revision),
          UNIQUE(id,run_id),
          CHECK(
            (
              status='sealed'
              AND reopened_at IS NULL
              AND reopened_lifecycle_revision IS NULL
              AND reopened_actor_type IS NULL
              AND reopened_actor_identifier IS NULL
              AND reopened_policy_version IS NULL
              AND reopened_reason_code IS NULL
              AND reopened_reason IS NULL
              AND reopened_transaction_id IS NULL
            )
            OR (
              status='reopened'
              AND reopened_at IS NOT NULL
              AND reopened_lifecycle_revision IS NOT NULL
              AND reopened_lifecycle_revision >= lifecycle_revision
              AND reopened_actor_type IS NOT NULL
              AND reopened_policy_version IS NOT NULL
              AND reopened_reason_code IS NOT NULL
              AND reopened_transaction_id IS NOT NULL
            )
          )
        );
        CREATE UNIQUE INDEX run_asset_membership_one_sealed_run_uk
          ON run_asset_membership_seals(run_id)
          WHERE status='sealed';
        CREATE INDEX run_asset_membership_seal_run_idx
          ON run_asset_membership_seals(run_id,seal_revision DESC);

        CREATE TABLE run_asset_membership_members(
          seal_id uuid NOT NULL,
          run_id uuid NOT NULL,
          subject_id uuid NOT NULL,
          snapshot_id uuid NOT NULL REFERENCES asset_snapshots(id) ON DELETE RESTRICT,
          role text NOT NULL,
          ordinal integer NOT NULL CHECK(ordinal >= 0),
          chunk_ids uuid[] NOT NULL,
          chunk_count bigint NOT NULL CHECK(chunk_count > 0),
          member_sha256 text NOT NULL CHECK(member_sha256 ~ '^[0-9a-f]{64}$'),
          PRIMARY KEY(seal_id,subject_id),
          UNIQUE(seal_id,ordinal),
          FOREIGN KEY(seal_id,run_id)
            REFERENCES run_asset_membership_seals(id,run_id) ON DELETE CASCADE,
          FOREIGN KEY(subject_id,run_id)
            REFERENCES run_asset_promotion_subjects(id,run_id) ON DELETE RESTRICT,
          FOREIGN KEY(subject_id,snapshot_id,role)
            REFERENCES run_asset_promotion_subjects(id,snapshot_id,role)
            ON DELETE RESTRICT,
          CHECK(cardinality(chunk_ids)=chunk_count)
        );
        CREATE INDEX run_asset_membership_member_snapshot_idx
          ON run_asset_membership_members(snapshot_id,role);

        ALTER TABLE indexing_checkpoints
          ADD COLUMN asset_membership_seal_id uuid
            REFERENCES run_asset_membership_seals(id),
          ADD COLUMN asset_membership_sha256 text,
          ADD COLUMN asset_expected_count integer,
          ADD COLUMN asset_expected_chunk_count bigint,
          ADD CONSTRAINT indexing_checkpoint_asset_membership_ck CHECK (
            (
              asset_membership_seal_id IS NULL
              AND asset_membership_sha256 IS NULL
              AND asset_expected_count IS NULL
              AND asset_expected_chunk_count IS NULL
            ) OR (
              asset_membership_seal_id IS NOT NULL
              AND asset_membership_sha256 IS NOT NULL
              AND asset_membership_sha256 ~ '^[0-9a-f]{64}$'
              AND asset_expected_count IS NOT NULL
              AND asset_expected_count > 0
              AND asset_expected_chunk_count IS NOT NULL
              AND asset_expected_chunk_count > 0
              AND asset_expected_chunk_count=expected_count
            )
          );
