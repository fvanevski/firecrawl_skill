"""PostgreSQL integration scenarios for issue #211."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID

import pytest

from asset_promotion_test_support import (
    TEST_DSN,
    AssetMembershipSealedError,
    AssetPromotionService,
    IndexCheckpointService,
    StoreConfig,
    _promote,
    _request,
    _seed_retained_assets,
    _subject_id_for_snapshot,
    _subject_rows,
    connect,
    promotion_config,
    uuid4,
)


def test_post_seal_addition_requires_reopen_then_reseal_with_revision_cas(
    promotion_config: StoreConfig,
):
    corpus, runs, status, _manifest = _seed_retained_assets(promotion_config)
    checkpoints = IndexCheckpointService(
        runs.uow_factory, max_attempts=promotion_config.max_index_attempts
    )
    checkpoint = checkpoints.ensure(
        status.id,
        lifecycle_revision=status.lifecycle_revision,
        fingerprint=checkpoints.active_fingerprint(status.id),
        idempotency_key=f"promotion:{status.id}:checkpoint-1",
    )
    first = checkpoints.asset_promotions.get_active_seal(status.id)
    assert first is not None

    late = corpus.ingest_batch(
        f"fc_late_{uuid4().hex}",
        "scrape",
        [_request("late-retained")],
        research_run_external_id=status.external_id,
    )
    assert late["failure_count"] == 0
    late_snapshot = UUID(str(late["assets"][0]["snapshot_id"]))
    late_subject = _subject_id_for_snapshot(status.id, late_snapshot)
    _promote(
        checkpoints.asset_promotions,
        late_subject,
        "evidence_eligible",
        status.lifecycle_revision,
    )
    with pytest.raises(
        AssetMembershipSealedError,
        match="reopen it before changing membership",
    ):
        _promote(
            checkpoints.asset_promotions,
            late_subject,
            "completion_critical",
            status.lifecycle_revision,
        )

    with pytest.raises(Exception, match="lifecycle revision is stale"):
        checkpoints.asset_promotions.reopen_completion_membership(
            status.id,
            expected_lifecycle_revision=status.lifecycle_revision + 1,
            actor_type="integration-test",
            actor_identifier="test_asset_promotion_integration",
            policy_version="test-reopen-v1",
            reason_code="late_asset",
            reason="wrong revision must fail",
        )
    reopened = checkpoints.asset_promotions.reopen_completion_membership(
        status.id,
        expected_lifecycle_revision=status.lifecycle_revision,
        actor_type="integration-test",
        actor_identifier="test_asset_promotion_integration",
        policy_version="test-reopen-v1",
        reason_code="late_asset",
        reason="explicitly reopen to add a late retained asset",
    )
    assert reopened.status == "reopened"
    assert checkpoints.get_active(status.id) is None
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT status,invalidation_reason FROM indexing_checkpoints WHERE id=%s",
            (checkpoint.id,),
        )
        assert cursor.fetchone() == ("invalidated", "asset_membership_reopened")

    _promote(
        checkpoints.asset_promotions,
        late_subject,
        "completion_critical",
        status.lifecycle_revision,
    )
    second = checkpoints.asset_promotions.seal_completion_membership(
        status.id,
        lifecycle_revision=status.lifecycle_revision,
        actor_type="integration-test",
        actor_identifier="test_asset_promotion_integration",
        policy_version="test-reopen-v1",
        reason_code="resealed_with_late_asset",
        reason="reseal exact membership after explicit reopen",
    )
    assert second.seal_revision == first.seal_revision + 1
    assert second.membership_sha256 != first.membership_sha256
    assert second.expected_asset_count == first.expected_asset_count + 1

    replacement = checkpoints.ensure(
        status.id,
        lifecycle_revision=status.lifecycle_revision,
        fingerprint=checkpoints.active_fingerprint(status.id),
        idempotency_key=f"promotion:{status.id}:checkpoint-1",
    )
    assert replacement.id != checkpoint.id
    assert replacement.entity_ids == second.chunk_ids


class _InterruptAfterOneStep(AssetPromotionService):
    def __init__(self, uow_factory):
        super().__init__(uow_factory)
        self.steps = 0

    def _after_promotion_step(self, step: tuple[UUID, str]) -> None:
        self.steps += 1
        if self.steps == 1:
            raise RuntimeError(f"injected interruption after {step[1]}")


def test_interrupted_promotion_resumes_from_last_durable_stage(
    promotion_config: StoreConfig,
):
    _corpus, runs, status, _manifest = _seed_retained_assets(promotion_config)
    interrupted = _InterruptAfterOneStep(runs.uow_factory)
    with pytest.raises(RuntimeError, match="injected interruption"):
        interrupted.prepare_for_indexing(
            status.id, lifecycle_revision=status.lifecycle_revision
        )
    assert _subject_rows(status.id)[0][2:] == (
        "evidence_eligible",
        1,
        "direct_retention",
    )

    resumed = AssetPromotionService(runs.uow_factory).prepare_for_indexing(
        status.id, lifecycle_revision=status.lifecycle_revision
    )
    assert resumed.expected_asset_count == 1
    assert _subject_rows(status.id)[0][2:] == (
        "completion_critical",
        2,
        "direct_retention",
    )


class _BlockingSealService(AssetPromotionService):
    def __init__(self, uow_factory, locked: Event, release: Event):
        super().__init__(uow_factory)
        self.locked = locked
        self.release = release

    def _after_membership_lock(self, run_id: UUID) -> None:
        self.locked.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError(f"test did not release membership lock for {run_id}")


def test_seal_and_completion_promotion_are_serialized_in_both_race_orders(
    promotion_config: StoreConfig,
):
    _corpus, runs, status, manifest = _seed_retained_assets(
        promotion_config, count=2
    )
    service = AssetPromotionService(runs.uow_factory)
    snapshots = [UUID(str(asset["snapshot_id"])) for asset in manifest["assets"]]
    first = _subject_id_for_snapshot(status.id, snapshots[0])
    second = _subject_id_for_snapshot(status.id, snapshots[1])
    for subject_id in (first, second):
        _promote(service, subject_id, "evidence_eligible", status.lifecycle_revision)
    _promote(service, first, "completion_critical", status.lifecycle_revision)

    locked = Event()
    release = Event()
    promotion_started = Event()
    blocking = _BlockingSealService(runs.uow_factory, locked, release)

    def late_completion():
        promotion_started.set()
        return _promote(
            AssetPromotionService(runs.uow_factory),
            second,
            "completion_critical",
            status.lifecycle_revision,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        seal_future = executor.submit(
            blocking.seal_completion_membership,
            status.id,
            lifecycle_revision=status.lifecycle_revision,
            actor_type="integration-test",
            actor_identifier="seal-wins",
            policy_version="test-race-v1",
            reason_code="seal_wins",
            reason="seal before concurrent completion promotion",
        )
        assert locked.wait(timeout=10)
        promotion_future = executor.submit(late_completion)
        assert promotion_started.wait(timeout=10)
        release.set()
        sealed = seal_future.result(timeout=10)
        with pytest.raises(AssetMembershipSealedError):
            promotion_future.result(timeout=10)
    assert sealed.expected_asset_count == 1

    service.reopen_completion_membership(
        status.id,
        expected_lifecycle_revision=status.lifecycle_revision,
        actor_type="integration-test",
        actor_identifier="race-reset",
        policy_version="test-race-v1",
        reason_code="race_reset",
        reason="prepare the inverse race ordering",
    )
    promotion_locked = Event()
    permit_commit = Event()
    seal_started = Event()

    def winning_completion():
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM research_runs WHERE id=%s FOR UPDATE",
                (status.id,),
            )
            cursor.execute(
                """UPDATE run_asset_promotion_subjects
                      SET current_stage='completion_critical',
                          actor_type='integration-test',
                          actor_identifier='promotion-wins',
                          policy_version='test-race-v1',
                          lifecycle_revision=%s,
                          reason_code='promotion_wins',
                          reason='promotion commits before sealing'
                    WHERE id=%s""",
                (status.lifecycle_revision, second),
            )
            promotion_locked.set()
            if not permit_commit.wait(timeout=10):
                raise TimeoutError("test did not permit promotion commit")

    def seal_after_promotion():
        seal_started.set()
        return AssetPromotionService(runs.uow_factory).seal_completion_membership(
            status.id,
            lifecycle_revision=status.lifecycle_revision,
            actor_type="integration-test",
            actor_identifier="promotion-wins",
            policy_version="test-race-v1",
            reason_code="promotion_wins",
            reason="seal after concurrent completion promotion",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        promotion_future = executor.submit(winning_completion)
        assert promotion_locked.wait(timeout=10)
        seal_future = executor.submit(seal_after_promotion)
        assert seal_started.wait(timeout=10)
        permit_commit.set()
        promotion_future.result(timeout=10)
        resealed = seal_future.result(timeout=10)
    assert resealed.expected_asset_count == 2
