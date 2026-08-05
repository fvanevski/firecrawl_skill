"""Issue #210 PostgreSQL integration tests.

These tests use the disposable database configured for the existing research
store integration suite. They do not reset the schema; each test creates unique
run and corpus identities and migrates idempotently to the current head.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from research_store.config import StoreConfig
from research_store.container import build_run_service, build_service
from research_store.domain import IngestRequest
from research_store.index_checkpoint_service import IndexCheckpointService
from research_store.postgres import connect, migrate

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


@pytest.fixture
def checkpoint_config(tmp_path: Path) -> StoreConfig:
    migrate(TEST_DSN)
    return replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection=f"checkpoint_{uuid4().hex}",
        embedding_dimension=4,
    )


def _seed_indexing_run(config: StoreConfig):
    runs = build_run_service(config)
    corpus = build_service(config)
    external_id = f"fr_checkpoint_{uuid4().hex}"
    status = runs.create(
        "issue 210 indexing checkpoint",
        external_id,
        execution_mode="autonomous_local",
    )
    manifest = corpus.ingest_batch(
        f"fc_checkpoint_{uuid4().hex}",
        "scrape",
        [
            IngestRequest(
                f"https://checkpoint.example/{uuid4().hex}",
                b"# Checkpoint evidence\n\nPostgreSQL owns exact membership.",
            )
        ],
        research_run_external_id=external_id,
    )
    assert manifest["failure_count"] == 0
    revision = status.lifecycle_revision
    for next_state in (
        "planning",
        "corpus_review",
        "acquiring",
        "extracting",
        "indexing",
    ):
        runs.transition(
            status.id,
            next_state,
            expected_revision=revision,
            idempotency_key=f"checkpoint-seed:{external_id}:{next_state}",
            actor_type="integration-test",
        )
        revision += 1
    current = runs.status(run_id=status.id)
    assert current.state == "indexing"
    return corpus, runs, current


def _mark_run_index_complete(run_id) -> None:
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE index_jobs job
                  SET status='complete', completed_at=now(), error=NULL,
                      lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL
                 FROM embedding_manifests manifest
                 JOIN chunks chunk ON chunk.id=manifest.chunk_id
                 JOIN documents document ON document.id=chunk.document_id
                 JOIN research_run_assets asset
                   ON asset.snapshot_id=document.snapshot_id
                WHERE job.manifest_id=manifest.id AND asset.run_id=%s""",
            (run_id,),
        )
        assert cursor.rowcount > 0
        cursor.execute(
            """UPDATE embedding_manifests manifest
                  SET index_status='complete', indexed_at=now(), error=NULL
                 FROM chunks chunk
                 JOIN documents document ON document.id=chunk.document_id
                 JOIN research_run_assets asset
                   ON asset.snapshot_id=document.snapshot_id
                WHERE manifest.chunk_id=chunk.id AND asset.run_id=%s""",
            (run_id,),
        )


def test_checkpoint_restart_reuses_exact_membership_and_records_observations(
    checkpoint_config: StoreConfig,
):
    _corpus, runs, status = _seed_indexing_run(checkpoint_config)
    first_service = IndexCheckpointService(
        runs.uow_factory, max_attempts=checkpoint_config.max_index_attempts
    )
    fingerprint = first_service.active_fingerprint(status.id)
    first = first_service.ensure(
        status.id,
        lifecycle_revision=status.lifecycle_revision,
        fingerprint=fingerprint,
        idempotency_key=f"checkpoint:{status.id}",
    )

    restarted_service = IndexCheckpointService(
        runs.uow_factory, max_attempts=checkpoint_config.max_index_attempts
    )
    restarted = restarted_service.ensure(
        status.id,
        lifecycle_revision=status.lifecycle_revision,
        fingerprint=fingerprint,
        idempotency_key=f"checkpoint:{status.id}",
    )
    assert restarted.id == first.id
    assert restarted.entity_ids == first.entity_ids
    assert restarted.expected_membership_sha256 == first.expected_membership_sha256

    first_finalization = restarted_service.finalize(
        status.id,
        restarted.id,
        expected_revision=status.lifecycle_revision,
        idempotency_key=f"checkpoint:{status.id}:finalize",
    )
    assert first_finalization.status == "recoverable"
    assert runs.status(run_id=status.id).state == "indexing"

    _mark_run_index_complete(status.id)
    resumed_finalization = restarted_service.finalize(
        status.id,
        restarted.id,
        expected_revision=status.lifecycle_revision,
        idempotency_key=f"checkpoint:{status.id}:finalize",
    )
    assert resumed_finalization.status == "advanced"
    assert runs.status(run_id=status.id).state == "coverage_review"

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT count(*),min(expected_count),max(expected_count)
                 FROM indexing_checkpoint_observations
                WHERE checkpoint_id=%s""",
            (first.id,),
        )
        observation_count, minimum, maximum = cursor.fetchone()
    assert observation_count >= 3
    assert minimum == maximum == len(first.entity_ids)


def test_late_retained_asset_does_not_change_sealed_checkpoint_membership(
    checkpoint_config: StoreConfig,
):
    corpus, runs, status = _seed_indexing_run(checkpoint_config)
    checkpoints = IndexCheckpointService(
        runs.uow_factory, max_attempts=checkpoint_config.max_index_attempts
    )
    checkpoint = checkpoints.ensure(
        status.id,
        lifecycle_revision=status.lifecycle_revision,
        fingerprint=checkpoints.active_fingerprint(status.id),
        idempotency_key=f"checkpoint:{status.id}",
    )
    sealed = checkpoints.asset_promotions.get_active_seal(status.id)
    assert sealed is not None

    added = corpus.ingest_batch(
        f"fc_checkpoint_growth_{uuid4().hex}",
        "scrape",
        [
            IngestRequest(
                f"https://checkpoint.example/growth/{uuid4().hex}",
                (
                    b"# Later retained evidence\n\n"
                    b"This remains outside the sealed barrier."
                ),
            )
        ],
        research_run_external_id=status.external_id,
    )
    assert added["failure_count"] == 0
    late_snapshot_id = str(added["assets"][0]["snapshot_id"])
    late_asset = next(
        asset
        for asset in checkpoints.asset_promotions.list_assets(status.id)
        if asset["snapshot_id"] == late_snapshot_id
    )
    assert late_asset["current_stage"] == "retained"

    still_sealed = checkpoints.asset_promotions.get_active_seal(status.id)
    assert still_sealed == sealed
    active = checkpoints.get_active(status.id)
    assert active is not None
    assert active.id == checkpoint.id
    assert active.entity_ids == sealed.chunk_ids

    _mark_run_index_complete(status.id)
    result = checkpoints.finalize(
        status.id,
        checkpoint.id,
        expected_revision=status.lifecycle_revision,
        idempotency_key=f"checkpoint:{status.id}:finalize",
    )
    assert result.status == "advanced"
    assert result.checkpoint.entity_ids == sealed.chunk_ids
    assert runs.status(run_id=status.id).state == "coverage_review"


def test_lifecycle_revision_change_invalidates_checkpoint_without_advancing(
    checkpoint_config: StoreConfig,
):
    _corpus, runs, status = _seed_indexing_run(checkpoint_config)
    checkpoints = IndexCheckpointService(
        runs.uow_factory, max_attempts=checkpoint_config.max_index_attempts
    )
    checkpoint = checkpoints.ensure(
        status.id,
        lifecycle_revision=status.lifecycle_revision,
        fingerprint=checkpoints.active_fingerprint(status.id),
        idempotency_key=f"checkpoint:{status.id}",
    )

    mode_change = runs.change_execution_mode(
        status.id,
        "agent_led",
        expected_revision=status.lifecycle_revision,
        idempotency_key=f"checkpoint:{status.id}:mode-change",
        requested_by="integration-test",
        approved_by="integration-test",
        reason="exercise checkpoint revision invalidation",
        actor_type="integration-test",
    )
    assert mode_change.lifecycle_revision == status.lifecycle_revision + 1
    assert runs.status(run_id=status.id).state == "indexing"

    result = checkpoints.finalize(
        status.id,
        checkpoint.id,
        expected_revision=status.lifecycle_revision,
        idempotency_key=f"checkpoint:{status.id}:finalize",
    )
    assert result.status == "invalidated"
    assert result.checkpoint.invalidation_reason == "lifecycle_revision_changed"
    current = runs.status(run_id=status.id)
    assert current.state == "indexing"
    assert current.lifecycle_revision == mode_change.lifecycle_revision


def test_concurrent_finalization_has_one_transition_and_idempotent_replay(
    checkpoint_config: StoreConfig,
):
    _corpus, runs, status = _seed_indexing_run(checkpoint_config)
    checkpoints = IndexCheckpointService(
        runs.uow_factory, max_attempts=checkpoint_config.max_index_attempts
    )
    checkpoint = checkpoints.ensure(
        status.id,
        lifecycle_revision=status.lifecycle_revision,
        fingerprint=checkpoints.active_fingerprint(status.id),
        idempotency_key=f"checkpoint:{status.id}",
    )
    _mark_run_index_complete(status.id)

    barrier = Barrier(2)

    def finalize_once(_ordinal: int):
        service = IndexCheckpointService(
            runs.uow_factory, max_attempts=checkpoint_config.max_index_attempts
        )
        barrier.wait(timeout=10)
        return service.finalize(
            status.id,
            checkpoint.id,
            expected_revision=status.lifecycle_revision,
            idempotency_key=f"checkpoint:{status.id}:finalize",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(finalize_once, range(2)))

    assert sorted(result.status for result in results) == ["advanced", "reused"]
    assert runs.status(run_id=status.id).state == "coverage_review"
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT count(*) FROM research_run_transitions
                WHERE run_id=%s AND next_state='coverage_review'
                  AND idempotency_key=%s""",
            (status.id, f"checkpoint:{status.id}:finalize"),
        )
        assert cursor.fetchone()[0] == 1


def test_terminal_transition_requires_decision_and_public_command_is_atomic(
    checkpoint_config: StoreConfig,
):
    runs = build_run_service(checkpoint_config)
    status = runs.create(
        "terminal decision guard",
        f"fr_terminal_guard_{uuid4().hex}",
        execution_mode="autonomous_local",
    )
    runs.transition(
        status.id,
        "planning",
        expected_revision=0,
        idempotency_key=f"terminal:{status.id}:planning",
        actor_type="integration-test",
    )

    with (
        pytest.raises(Exception, match="terminal transition requires"),
        runs.uow_factory() as uow,
    ):
        uow.runs.apply_run_transition(
            status.id,
            "failed",
            1,
            f"terminal:{status.id}:bypass",
            "integration-test",
            "run-state-v1",
            permitted_prior_states=frozenset({"planning"}),
            event_type="run.transitioned.failed",
            reason="attempted decision bypass",
            outcome="failed",
            error="attempted decision bypass",
        )

    result = runs.fail(
        status.id,
        expected_revision=1,
        idempotency_key=f"terminal:{status.id}:guarded",
        actor_type="integration-test",
        reason="structured terminal failure",
        outcome="failed",
        error="structured terminal failure",
        completion={
            "reason_code": "integration_test_failure",
            "state_census": {
                "schema_version": "terminal-state-census-v1",
                "available": True,
                "counts": {"failed": 1},
            },
        },
    )
    assert result.next_state == "failed"
    assert runs.status(run_id=status.id).state == "failed"

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT count(*),min(reason_code),min(state_census->>'schema_version')
                 FROM terminal_decisions
                WHERE run_id=%s AND idempotency_key=%s""",
            (status.id, f"terminal:{status.id}:guarded"),
        )
        count, reason_code, schema_version = cursor.fetchone()
        assert (count, reason_code, schema_version) == (
            1,
            "integration_test_failure",
            "terminal-state-census-v1",
        )
