"""PostgreSQL integration scenarios for issue #211."""

from __future__ import annotations

# ruff: noqa: F403, F405

from asset_promotion_test_support import *  # noqa: F403


def test_full_stage_path_is_explicit_and_extraction_does_not_auto_admit(
    promotion_config: StoreConfig,
):
    runs = build_run_service(promotion_config)
    corpus = build_service(promotion_config)
    status = runs.create(
        "full promotion path",
        f"fr_full_promotion_{uuid4().hex}",
        execution_mode="autonomous_local",
    )
    candidate_id = _insert_candidate(status.id, "full-path")

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        with pytest.raises(Exception, match="invalid asset promotion transition"):
            cursor.execute(
                """UPDATE run_asset_promotion_subjects
                      SET current_stage='extracted',actor_type='integration-test',
                          actor_identifier='invalid-skip',policy_version='test-v1',
                          lifecycle_revision=%s,reason_code='invalid_skip',
                          reason='must not skip selected_for_extraction'
                    WHERE run_id=%s AND candidate_id=%s""",
                (status.lifecycle_revision, status.id, candidate_id),
            )
        connection.rollback()

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO extraction_attempts(
                   candidate_id,run_id,attempt_number,method,method_version,
                   start_time,end_time,exit_status,failure_class,disposition,
                   selected,selection_reason)
                 VALUES(%s,%s,1,'firecrawl_main_content','integration-test',
                        now(),now(),'succeeded','none','acceptable',true,
                        'integration test') RETURNING id""",
            (candidate_id, status.id),
        )
        attempt_id = UUID(str(cursor.fetchone()[0]))

    ingest = corpus.ingest(_request("full-path"))
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE asset_snapshots SET extraction_attempt_id=%s WHERE id=%s",
            (attempt_id, ingest.snapshot_id),
        )
        cursor.execute(
            """INSERT INTO research_run_assets(run_id,snapshot_id,role,metadata)
                 VALUES(%s,%s,'acquired','{}')""",
            (status.id, ingest.snapshot_id),
        )
        cursor.execute(
            """SELECT current_stage FROM run_asset_promotion_subjects
                WHERE run_id=%s AND candidate_id=%s""",
            (status.id, candidate_id),
        )
        assert cursor.fetchone()[0] == "retained"

    current = _advance_to_indexing(runs, status)
    service = AssetPromotionService(runs.uow_factory)
    seal = service.prepare_for_indexing(
        status.id, lifecycle_revision=current.lifecycle_revision
    )
    assert seal.expected_asset_count == 1

    events = service.list_events(status.id)
    assert [event["to_stage"] for event in events] == [
        "discovered",
        "selected_for_extraction",
        "extracted",
        "retained",
        "evidence_eligible",
        "completion_critical",
    ]
    assert all(event["actor_type"] for event in events)
    assert all(event["policy_version"] for event in events)
    assert all(event["reason_code"] for event in events)


def test_only_completion_critical_assets_contribute_to_sealed_chunks(
    promotion_config: StoreConfig,
):
    _corpus, runs, status, manifest = _seed_retained_assets(
        promotion_config, count=2
    )
    service = AssetPromotionService(runs.uow_factory)
    snapshots = [UUID(asset["snapshot_id"]) for asset in manifest["assets"]]
    included = _subject_id_for_snapshot(status.id, snapshots[0])
    excluded = _subject_id_for_snapshot(status.id, snapshots[1])

    _promote(service, included, "evidence_eligible", status.lifecycle_revision)
    _promote(service, included, "completion_critical", status.lifecycle_revision)
    service.reject(
        excluded,
        expected_lifecycle_revision=status.lifecycle_revision,
        actor_type="integration-test",
        actor_identifier="test_asset_promotion_integration",
        policy_version="test-promotion-v1",
        reason_code="not_completion_critical",
        reason="explicitly excluded from the completion barrier",
    )
    seal = service.seal_completion_membership(
        status.id,
        lifecycle_revision=status.lifecycle_revision,
        actor_type="integration-test",
        actor_identifier="test_asset_promotion_integration",
        policy_version="test-promotion-v1",
        reason_code="test_seal",
        reason="seal only the explicitly admitted asset",
    )
    assert seal.expected_asset_count == 1
    assert seal.members[0].subject_id == included
    assert seal.expected_chunk_count == len(seal.chunk_ids)

    checkpoint = IndexCheckpointService(
        runs.uow_factory, max_attempts=promotion_config.max_index_attempts
    ).ensure(
        status.id,
        lifecycle_revision=status.lifecycle_revision,
        fingerprint=IndexCheckpointService(runs.uow_factory).active_fingerprint(
            status.id
        ),
        idempotency_key=f"promotion:{status.id}:checkpoint",
    )
    assert checkpoint.entity_ids == seal.chunk_ids


def test_shared_snapshot_roles_count_chunks_once_in_checkpoint_membership(
    promotion_config: StoreConfig,
):
    _corpus, runs, status, manifest = _seed_retained_assets(promotion_config)
    snapshot_id = UUID(str(manifest["assets"][0]["snapshot_id"]))
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO research_run_assets(run_id,snapshot_id,role,metadata)
                 VALUES(%s,%s,'citation','{}')""",
            (status.id, snapshot_id),
        )

    service = AssetPromotionService(runs.uow_factory)
    seal = service.prepare_for_indexing(
        status.id, lifecycle_revision=status.lifecycle_revision
    )
    assert seal.expected_asset_count == 2
    assert seal.expected_chunk_count == len(seal.chunk_ids)
    assert sum(len(member.chunk_ids) for member in seal.members) > len(
        seal.chunk_ids
    )

    checkpoints = IndexCheckpointService(
        runs.uow_factory, max_attempts=promotion_config.max_index_attempts
    )
    checkpoint = checkpoints.ensure(
        status.id,
        lifecycle_revision=status.lifecycle_revision,
        fingerprint=checkpoints.active_fingerprint(status.id),
        idempotency_key=f"promotion:{status.id}:shared-snapshot",
    )
    assert checkpoint.entity_ids == seal.chunk_ids
    assert checkpoint.expected_count == seal.expected_chunk_count


def test_sealing_is_idempotent_hash_addressed_and_completion_payload_is_queryable(
    promotion_config: StoreConfig,
):
    _corpus, runs, status, _manifest = _seed_retained_assets(
        promotion_config, count=2
    )
    checkpoints = IndexCheckpointService(
        runs.uow_factory, max_attempts=promotion_config.max_index_attempts
    )
    checkpoint = checkpoints.ensure(
        status.id,
        lifecycle_revision=status.lifecycle_revision,
        fingerprint=checkpoints.active_fingerprint(status.id),
        idempotency_key=f"promotion:{status.id}:checkpoint",
    )
    first = checkpoints.asset_promotions.get_active_seal(status.id)
    assert first is not None
    second = checkpoints.asset_promotions.prepare_for_indexing(
        status.id, lifecycle_revision=status.lifecycle_revision
    )
    assert second == first
    assert checkpoint.entity_ids == first.chunk_ids
    assert checkpoint.expected_count == first.expected_chunk_count

    _mark_run_index_complete(status.id)
    result = checkpoints.finalize(
        status.id,
        checkpoint.id,
        expected_revision=status.lifecycle_revision,
        idempotency_key=f"promotion:{status.id}:finalize",
    )
    assert result.status == "advanced"
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT completion->>'asset_membership_seal_id',
                      completion->>'asset_membership_sha256',
                      (completion->>'asset_expected')::integer,
                      (completion->>'asset_expected_chunk_count')::integer
                 FROM research_run_transitions
                WHERE run_id=%s AND next_state='coverage_review'
                ORDER BY lifecycle_revision DESC LIMIT 1""",
            (status.id,),
        )
        assert cursor.fetchone() == (
            str(first.id),
            first.membership_sha256,
            first.expected_asset_count,
            first.expected_chunk_count,
        )
