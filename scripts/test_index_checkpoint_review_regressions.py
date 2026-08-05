"""Production-seam regressions for the independent review of issue #210."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

from research_domain.models import TerminalDecision, TerminalDecisionOutcome
from research_store.config import StoreConfig
from research_store.container import (
    build_run_service,
    build_service,
    build_workflow_operation_service,
)
from research_store.domain import IngestRequest
from research_store.index_checkpoint_service import IndexCheckpointService
from research_store.postgres import connect, migrate
from research_store.terminal_decision_service import (
    TerminalDecisionError,
    TerminalDecisionService,
)
from research_store.workflow_service import WorkflowBoundaryError
from resume_index_checkpoint import main as resume_checkpoint_main

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
        qdrant_collection=f"checkpoint_review_{uuid4().hex}",
        embedding_dimension=4,
    )


@pytest.fixture
def pre_v39_database(tmp_path: Path):
    import psycopg
    from psycopg import sql

    parsed = urlsplit(TEST_DSN)
    database_name = f"firecrawl_checkpoint_migration_test_{uuid4().hex}"
    admin_dsn = urlunsplit(
        (parsed.scheme, parsed.netloc, "/postgres", parsed.query, parsed.fragment)
    )
    test_dsn = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"/{database_name}",
            parsed.query,
            parsed.fragment,
        )
    )

    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    try:
        migrate(test_dsn, "0038_postgres_authority")
        yield test_dsn, tmp_path / "legacy-blobs"
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=%s AND pid<>pg_backend_pid()",
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(database_name)
                )
            )


def _seed_indexing_run(config: StoreConfig):
    runs = build_run_service(config)
    corpus = build_service(config)
    external_id = f"fr_checkpoint_review_{uuid4().hex}"
    status = runs.create(
        "issue 210 review regression",
        external_id,
        execution_mode="autonomous_local",
    )
    manifest = corpus.ingest_batch(
        f"fc_checkpoint_review_{uuid4().hex}",
        "scrape",
        [
            IngestRequest(
                f"https://checkpoint-review.example/{uuid4().hex}",
                b"# Review regression\n\nPostgreSQL owns exact membership.",
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
            idempotency_key=f"review-seed:{external_id}:{next_state}",
            actor_type="integration-test",
        )
        revision += 1
    current = runs.status(run_id=status.id)
    assert current.state == "indexing"
    return corpus, runs, current


def _seed_planning_run(config: StoreConfig):
    runs = build_run_service(config)
    status = runs.create(
        "terminal decision review regression",
        f"fr_terminal_review_{uuid4().hex}",
        execution_mode="autonomous_local",
    )
    runs.transition(
        status.id,
        "planning",
        expected_revision=status.lifecycle_revision,
        idempotency_key=f"review-terminal:{status.id}:planning",
        actor_type="integration-test",
    )
    return runs, runs.status(run_id=status.id)


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


def _insert_structured_decision(cursor, status, key: str, *, outcome: str) -> None:
    cursor.execute(
        """INSERT INTO terminal_decisions(
               run_id,decision_id,run_revision,coverage_revision,outcome,
               no_progress_signals,unresolved_gap,policy_version,
               idempotency_key,created_at,reason_code,state_census)
             VALUES(%s,%s,%s,0,%s,%s,%s,%s,%s,now(),%s,%s)""",
        (
            status.id,
            uuid4(),
            status.lifecycle_revision,
            outcome,
            ["review_regression"],
            "review regression",
            "terminal-lifecycle-v2",
            key,
            "review_regression",
            json.dumps(
                {
                    "schema_version": "terminal-state-census-v1",
                    "available": True,
                    "counts": {outcome: 1},
                }
            ),
        ),
    )


def test_v39_backfills_only_preexisting_decisions_and_removes_legacy_defaults(
    pre_v39_database,
):
    database_url, blob_root = pre_v39_database
    config = replace(
        StoreConfig.from_env(),
        database_url=database_url,
        blob_root=blob_root,
    )
    runs, status = _seed_planning_run(config)
    legacy_key = f"legacy:{status.id}"
    with connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO terminal_decisions(
                   run_id,decision_id,run_revision,coverage_revision,outcome,
                   no_progress_signals,unresolved_gap,policy_version,
                   idempotency_key,created_at)
                 VALUES(%s,%s,%s,0,'failed',%s,%s,%s,%s,now())""",
            (
                status.id,
                uuid4(),
                status.lifecycle_revision,
                ["legacy_observation"],
                "pre-v39 terminal observation",
                "terminal-decision-policy-v1",
                legacy_key,
            ),
        )

    migrate(database_url)
    with connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT reason_code,state_census->>'reason',
                      decision_transaction_id IS NULL
                 FROM terminal_decisions
                WHERE run_id=%s AND idempotency_key=%s""",
            (status.id, legacy_key),
        )
        assert cursor.fetchone() == ("legacy_unstructured", "legacy_unstructured", True)
        cursor.execute(
            """SELECT column_name,column_default
                 FROM information_schema.columns
                WHERE table_schema=current_schema()
                  AND table_name='terminal_decisions'
                  AND column_name IN ('reason_code','state_census')
                ORDER BY column_name"""
        )
        assert cursor.fetchall() == [("reason_code", None), ("state_census", None)]


def test_orphan_terminal_decision_cannot_commit(checkpoint_config: StoreConfig):
    _runs, status = _seed_planning_run(checkpoint_config)
    key = f"review-orphan:{status.id}"

    with pytest.raises(Exception, match="matching terminal transition"):
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            _insert_structured_decision(cursor, status, key, outcome="failed")

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM terminal_decisions "
            "WHERE run_id=%s AND idempotency_key=%s",
            (status.id, key),
        )
        assert cursor.fetchone()[0] == 0


def test_semantically_mismatched_terminal_command_rolls_back(
    checkpoint_config: StoreConfig,
):
    runs, status = _seed_planning_run(checkpoint_config)
    key = f"review-mismatch:{status.id}"

    with pytest.raises(Exception, match="does not authorize"):
        with runs.uow_factory() as uow:
            with uow.connection.cursor() as cursor:
                _insert_structured_decision(cursor, status, key, outcome="partial")
            uow.runs.apply_run_transition(
                status.id,
                "failed",
                status.lifecycle_revision,
                key,
                "integration-test",
                "run-state-v1",
                permitted_prior_states=frozenset({"planning"}),
                event_type="run.transitioned.failed",
                reason="mismatched terminal command",
                outcome="failed",
                error="mismatched terminal command",
            )

    current = runs.status(run_id=status.id)
    assert (current.state, current.lifecycle_revision) == (
        status.state,
        status.lifecycle_revision,
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT
                 (SELECT count(*) FROM terminal_decisions
                   WHERE run_id=%s AND idempotency_key=%s),
                 (SELECT count(*) FROM research_run_transitions
                   WHERE run_id=%s AND idempotency_key=%s),
                 (SELECT count(*) FROM research_events
                   WHERE run_id=%s AND idempotency_key=%s)""",
            (status.id, key, status.id, key, status.id, key),
        )
        assert cursor.fetchone() == (0, 0, 0)


def test_guarded_terminal_command_persists_one_atomic_semantic_pair(
    checkpoint_config: StoreConfig,
):
    runs, status = _seed_planning_run(checkpoint_config)
    key = f"review-atomic:{status.id}"
    result = runs.fail(
        status.id,
        expected_revision=status.lifecycle_revision,
        idempotency_key=key,
        actor_type="integration-test",
        reason="structured terminal failure",
        outcome="failed",
        error="structured terminal failure",
        completion={
            "reason_code": "review_structured_failure",
            "state_census": {
                "schema_version": "terminal-state-census-v1",
                "available": True,
                "counts": {"failed": 1},
            },
        },
    )
    assert result.next_state == "failed"

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT decision.reason_code,
                      decision.decision_transaction_id::text,
                      transition.transition_transaction_id::text,
                      decision.run_revision,transition.lifecycle_revision,
                      decision.outcome::text,transition.next_state
                 FROM terminal_decisions decision
                 JOIN research_run_transitions transition
                   ON transition.run_id=decision.run_id
                  AND transition.idempotency_key=decision.idempotency_key
                WHERE decision.run_id=%s AND decision.idempotency_key=%s""",
            (status.id, key),
        )
        row = cursor.fetchone()
    assert row[0] == "review_structured_failure"
    assert row[1] == row[2]
    assert row[4] == row[3] + 1
    assert row[5:] == ("failed", "failed")


def test_concurrent_identical_terminal_commands_reuse_one_atomic_pair(
    checkpoint_config: StoreConfig,
):
    runs, status = _seed_planning_run(checkpoint_config)
    key = f"review-concurrent-terminal:{status.id}"
    barrier = Barrier(2)

    def fail_once(_ordinal: int):
        service = build_run_service(checkpoint_config)
        barrier.wait(timeout=10)
        return service.fail(
            status.id,
            expected_revision=status.lifecycle_revision,
            idempotency_key=key,
            actor_type="integration-test",
            reason="concurrent structured terminal failure",
            outcome="failed",
            error="concurrent structured terminal failure",
            completion={
                "reason_code": "review_concurrent_failure",
                "state_census": {
                    "schema_version": "terminal-state-census-v1",
                    "available": True,
                    "counts": {"failed": 1},
                },
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(fail_once, range(2)))

    assert sorted(result.reused for result in results) == [False, True]
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT
                 (SELECT count(*) FROM terminal_decisions
                   WHERE run_id=%s AND idempotency_key=%s),
                 (SELECT count(*) FROM research_run_transitions
                   WHERE run_id=%s AND idempotency_key=%s),
                 (SELECT min(decision_transaction_id::text)
                    FROM terminal_decisions
                   WHERE run_id=%s AND idempotency_key=%s),
                 (SELECT min(transition_transaction_id::text)
                    FROM research_run_transitions
                   WHERE run_id=%s AND idempotency_key=%s)""",
            (
                status.id,
                key,
                status.id,
                key,
                status.id,
                key,
                status.id,
                key,
            ),
        )
        decision_count, transition_count, decision_xid, transition_xid = (
            cursor.fetchone()
        )
    assert (decision_count, transition_count) == (1, 1)
    assert decision_xid == transition_xid


def test_standalone_terminal_decision_service_fails_before_writing(
    checkpoint_config: StoreConfig,
):
    runs, status = _seed_planning_run(checkpoint_config)
    key = f"review-standalone:{status.id}"
    decision = TerminalDecision(
        schema_version=TerminalDecision.SCHEMA_VERSION,
        decision_id=uuid4(),
        run_id=status.id,
        run_revision=status.lifecycle_revision,
        coverage_revision=0,
        outcome=TerminalDecisionOutcome.FAILED,
        no_progress_signals=(),
        unresolved_gap="standalone writes are not authoritative",
        policy_version=TerminalDecision.POLICY_VERSION,
        created_at=datetime.now(timezone.utc),
    )

    with pytest.raises(TerminalDecisionError, match="standalone.*prohibited"):
        TerminalDecisionService(runs.uow_factory).record(status.id, decision, key)

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM terminal_decisions "
            "WHERE run_id=%s AND idempotency_key=%s",
            (status.id, key),
        )
        assert cursor.fetchone()[0] == 0


def test_public_resume_replays_completed_checkpoint_without_new_transition(
    checkpoint_config: StoreConfig,
    monkeypatch,
    capsys,
):
    _corpus, runs, status = _seed_indexing_run(checkpoint_config)
    checkpoints = IndexCheckpointService(
        runs.uow_factory, max_attempts=checkpoint_config.max_index_attempts
    )
    checkpoint = checkpoints.ensure(
        status.id,
        lifecycle_revision=status.lifecycle_revision,
        fingerprint=checkpoints.active_fingerprint(status.id),
        idempotency_key=f"review-checkpoint:{status.id}",
    )
    _mark_run_index_complete(status.id)
    first = checkpoints.finalize(
        status.id,
        checkpoint.id,
        expected_revision=status.lifecycle_revision,
        idempotency_key=f"index-checkpoint:{checkpoint.id}:finalize",
        actor_type="wrapper",
        actor_identifier="integration-test",
    )
    assert first.status == "advanced"

    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("BLOB_ROOT", str(checkpoint_config.blob_root))
    for _attempt in range(2):
        assert resume_checkpoint_main([status.external_id]) == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["status"] == "complete"
        assert payload["reason"] == "completed_checkpoint_replayed"
        assert payload["finalization"]["status"] == "reused"
        assert payload["checkpoint"]["id"] == str(checkpoint.id)

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT count(*) FROM research_run_transitions
                WHERE run_id=%s AND prior_state='indexing'
                  AND next_state='coverage_review'""",
            (status.id,),
        )
        assert cursor.fetchone()[0] == 1


def test_invalid_wrapper_operation_is_rejected_without_any_mutation(
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
        idempotency_key=f"review-wrapper:{status.id}",
    )
    _mark_run_index_complete(status.id)

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT
                 (SELECT count(*) FROM research_run_transitions WHERE run_id=%s),
                 (SELECT count(*) FROM research_invocations WHERE run_id=%s)""",
            (status.id, status.id),
        )
        transition_count, invocation_count = cursor.fetchone()

    workflow = build_workflow_operation_service(checkpoint_config)
    with pytest.raises(WorkflowBoundaryError, match="unsupported wrapper operation"):
        workflow.begin_operation(
            status.external_id,
            f"fc_invalid_{uuid4().hex}",
            "unsupported-operation",
            {},
        )
    with pytest.raises(WorkflowBoundaryError, match="external_invocation_id"):
        workflow.begin_operation(status.external_id, "", "fsearch", {})
    with pytest.raises(WorkflowBoundaryError, match="input_data"):
        workflow.begin_operation(
            status.external_id,
            f"fc_invalid_input_{uuid4().hex}",
            "fsearch",
            ["not", "an", "object"],
        )

    current = runs.status(run_id=status.id)
    reloaded = checkpoints.get_active(status.id)
    assert (current.state, current.lifecycle_revision) == (
        status.state,
        status.lifecycle_revision,
    )
    assert reloaded is not None
    assert (reloaded.id, reloaded.status) == (checkpoint.id, "active")
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT
                 (SELECT count(*) FROM research_run_transitions WHERE run_id=%s),
                 (SELECT count(*) FROM research_invocations WHERE run_id=%s)""",
            (status.id, status.id),
        )
        assert cursor.fetchone() == (transition_count, invocation_count)
