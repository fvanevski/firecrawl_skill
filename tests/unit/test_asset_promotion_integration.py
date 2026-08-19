"""PostgreSQL integration scenarios for issue #211."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from asset_promotion_test_support import (
    TEST_DSN,
    _advance_to_indexing,
    _insert_candidate,
    _mark_run_index_complete,
    _promote,
    _request,
    _seed_retained_assets,
    _subject_id_for_snapshot,
)
from research_store.asset_promotion_models import _canonical_sha256, _member_payload
from research_store.asset_promotion_service import AssetPromotionService
from research_store.config import StoreConfig
from research_store.container import (
    build_extraction_service,
    build_run_service,
    build_service,
)
from research_store.index_checkpoint_service import IndexCheckpointService
from research_store.postgres import connect

pytest_plugins = ("asset_promotion_test_support",)


def test_full_stage_path_is_explicit_and_extraction_does_not_auto_admit(
    promotion_config: StoreConfig,
):
    runs = build_run_service(promotion_config)
    corpus = build_service(promotion_config)
    extraction = build_extraction_service(promotion_config)
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

    request = _request("full-path")
    attempt_id = extraction.create_attempt(
        candidate_id=candidate_id,
        run_id=status.id,
        method_version="integration-test",
        requested_format="markdown",
    )
    service = AssetPromotionService(runs.uow_factory)
    assert [event["to_stage"] for event in service.list_events(status.id)] == [
        "discovered",
        "selected_for_extraction",
    ]

    raw_blob = extraction.store_raw_blob(request.content)
    normalized_blob = extraction.store_normalized_blob(
        request.normalized_content or request.content
    )
    extraction.complete_attempt(
        attempt_id,
        "succeeded",
        raw_blob=raw_blob,
        normalized_blob=normalized_blob,
        parser_used=promotion_config.parser_version,
    )
    ingest = corpus.ingest(replace(request, extraction_attempt_id=attempt_id))
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
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


@pytest.mark.parametrize("exit_status", ["failed", "partial", "cancelled"])
def test_non_successful_final_extraction_never_records_extracted_provenance(
    promotion_config: StoreConfig,
    exit_status: str,
):
    runs = build_run_service(promotion_config)
    extraction = build_extraction_service(promotion_config)
    status = runs.create(
        f"truthful extraction {exit_status}",
        f"fr_truthful_{exit_status}_{uuid4().hex}",
        execution_mode="autonomous_local",
    )
    candidate_id = _insert_candidate(status.id, exit_status)
    attempt_id = extraction.create_attempt(candidate_id, status.id)
    extraction.complete_attempt(
        attempt_id,
        exit_status,
        failure_class="internal",
        error_message=f"final {exit_status} result",
    )
    events = AssetPromotionService(runs.uow_factory).list_events(status.id)
    assert [event["to_stage"] for event in events] == [
        "discovered",
        "selected_for_extraction",
    ]


def test_finalized_success_requires_persisted_output_and_becomes_immutable(
    promotion_config: StoreConfig,
):
    runs = build_run_service(promotion_config)
    extraction = build_extraction_service(promotion_config)
    status = runs.create(
        "truthful successful extraction",
        f"fr_truthful_success_{uuid4().hex}",
        execution_mode="autonomous_local",
    )
    candidate_id = _insert_candidate(status.id, "success")
    attempt_id = extraction.create_attempt(candidate_id, status.id)
    payload = b"authoritative extraction output"
    raw_blob = extraction.store_raw_blob(payload)
    extraction.complete_attempt(attempt_id, "succeeded", raw_blob=raw_blob)
    events = AssetPromotionService(runs.uow_factory).list_events(status.id)
    assert [event["to_stage"] for event in events] == [
        "discovered",
        "selected_for_extraction",
        "extracted",
    ]
    with pytest.raises(Exception, match="immutable promotion provenance"):
        extraction.complete_attempt(
            attempt_id,
            "failed",
            failure_class="internal",
            error_message="attempted provenance rewrite",
        )


def test_only_completion_critical_assets_contribute_to_sealed_chunks(
    promotion_config: StoreConfig,
):
    _corpus, runs, status, manifest = _seed_retained_assets(promotion_config, count=2)
    service = AssetPromotionService(runs.uow_factory)
    snapshots = [UUID(str(asset["snapshot_id"])) for asset in manifest["assets"]]
    included = _subject_id_for_snapshot(status.id, snapshots[0])
    excluded = _subject_id_for_snapshot(status.id, snapshots[1])

    _promote(service, included, "evidence_eligible", status.lifecycle_revision)
    service.candidate_policy_service.evaluate_completion_admission(
        status.id,
        status.lifecycle_revision,
        service.candidate_budget,
    )
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
    assert sum(len(member.chunk_ids) for member in seal.members) > len(seal.chunk_ids)

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


def test_database_rejects_non_addressing_membership_rows(
    promotion_config: StoreConfig,
):
    _corpus, runs, status, manifest = _seed_retained_assets(promotion_config)
    snapshot_id = UUID(str(manifest["assets"][0]["snapshot_id"]))
    service = AssetPromotionService(runs.uow_factory)
    subject_id = _subject_id_for_snapshot(status.id, snapshot_id)
    _promote(service, subject_id, "evidence_eligible", status.lifecycle_revision)
    service.candidate_policy_service.evaluate_completion_admission(
        status.id,
        status.lifecycle_revision,
        service.candidate_budget,
    )
    _promote(service, subject_id, "completion_critical", status.lifecycle_revision)
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT subject.role,array_agg(chunk.id ORDER BY chunk.id)
                 FROM run_asset_promotion_subjects subject
                 JOIN documents document ON document.snapshot_id=subject.snapshot_id
                 JOIN chunks chunk ON chunk.document_id=document.id
                WHERE subject.id=%s GROUP BY subject.role""",
            (subject_id,),
        )
        role, raw_chunk_ids = cursor.fetchone()
    chunk_ids = tuple(UUID(str(chunk_id)) for chunk_id in raw_chunk_ids)
    member_payload = _member_payload(subject_id, snapshot_id, role, chunk_ids)
    member_sha256 = _canonical_sha256(member_payload)
    membership_sha256 = _canonical_sha256([member_payload])

    cases = (
        ("0" * 64, membership_sha256, chunk_ids, "member SHA-256"),
        (member_sha256, "0" * 64, chunk_ids, "seal SHA-256"),
        (
            _canonical_sha256(
                _member_payload(
                    subject_id, snapshot_id, role, (chunk_ids[0], chunk_ids[0])
                )
            ),
            _canonical_sha256(
                [
                    _member_payload(
                        subject_id, snapshot_id, role, (chunk_ids[0], chunk_ids[0])
                    )
                ]
            ),
            (chunk_ids[0], chunk_ids[0]),
            "non-null, unique, and sorted",
        ),
    )
    for member_hash, seal_hash, stored_chunks, message in cases:
        with (
            pytest.raises(Exception, match=message),
            connect(TEST_DSN) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """INSERT INTO run_asset_membership_seals(
                       run_id,seal_revision,lifecycle_revision,
                       membership_sha256,expected_asset_count,
                       expected_chunk_count,actor_type,actor_identifier,
                       policy_version,reason_code,reason)
                     VALUES(%s,1,%s,%s,1,%s,'integration-test',
                       'negative-database-test','test-v1','invalid_seal',
                       'the database must reject this row') RETURNING id""",
                (
                    status.id,
                    status.lifecycle_revision,
                    seal_hash,
                    len(set(stored_chunks)),
                ),
            )
            seal_id = cursor.fetchone()[0]
            cursor.execute(
                """INSERT INTO run_asset_membership_members(
                       seal_id,run_id,subject_id,snapshot_id,role,ordinal,
                       chunk_ids,chunk_count,member_sha256)
                     VALUES(%s,%s,%s,%s,%s,0,%s,%s,%s)""",
                (
                    seal_id,
                    status.id,
                    subject_id,
                    snapshot_id,
                    role,
                    list(stored_chunks),
                    len(stored_chunks),
                    member_hash,
                ),
            )
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_sealing_is_idempotent_hash_addressed_and_completion_payload_is_queryable(
    promotion_config: StoreConfig,
):
    _corpus, runs, status, _manifest = _seed_retained_assets(promotion_config, count=2)
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
            """SELECT validation_result->'completion'->>'asset_membership_seal_id',
                      validation_result->'completion'->>'asset_membership_sha256',
                      (validation_result->'completion'->>'asset_expected')::integer,
                      (validation_result->'completion'
                        ->>'asset_expected_chunk_count')::integer
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
