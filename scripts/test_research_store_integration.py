"""Opt-in PostgreSQL integration tests.

Set RESEARCH_STORE_TEST_DATABASE_URL to a disposable PostgreSQL database whose
name contains a standalone ``test`` segment, and set
RESEARCH_STORE_TEST_ALLOW_RESET to that exact database name. The suite never
guesses or reuses DATABASE_URL because its session setup drops the public schema.
"""

from __future__ import annotations

# ruff: noqa: E402 - load the sibling script package without installing it.

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.config import StoreConfig
from research_store import cli as store_cli
from research_store.container import build_service
from research_store.domain import IngestRequest
from research_store.postgres import connect, migrate, require_disposable_database_reset
from research_store.run_service import (
    ResearchRunService,
    RunStateError,
    StaleRunRevisionError,
)
from research_store.legacy_adapter import AdapterMode, LegacyEntryPointAdapter
from research_store.semantic_service import SemanticCallService


TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


@pytest.fixture
def service(tmp_path, prepared_database):
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection="research_integration_test",
        embedding_dimension=4,
    )
    return build_service(config)


@pytest.fixture(scope="session")
def prepared_database():
    """Prove fresh and populated prior-head migrations without data loss."""
    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    assert migrate(TEST_DSN) == 8
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT to_regclass('research_run_transitions'),
            to_regclass('research_events'),to_regclass('semantic_artifacts'),
            to_regclass('research_budget_snapshots')"""
        )
        assert all(cursor.fetchone())

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    assert migrate(TEST_DSN, "0001_research_store") == 1

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO sources(canonical_url) VALUES (%s) RETURNING id",
            ("https://integration.example/legacy-multi-index",),
        )
        source_id = cursor.fetchone()[0]
        cursor.execute(
            """INSERT INTO asset_snapshots(
                source_id,requested_url,retrieved_at,content_sha256
            ) VALUES (%s,%s,now(),%s) RETURNING id""",
            (source_id, "https://integration.example/legacy-multi-index", "a" * 64),
        )
        snapshot_id = cursor.fetchone()[0]
        cursor.execute(
            """INSERT INTO documents(
                snapshot_id,normalized_text,parser_name,parser_version,
                normalization_version,document_sha256
            ) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
            (snapshot_id, "legacy evidence", "legacy", "1", "1", "b" * 64),
        )
        document_id = cursor.fetchone()[0]
        cursor.execute(
            """INSERT INTO chunks(
                document_id,ordinal,text,content_sha256,chunker_name,chunker_version
            ) VALUES (%s,0,%s,%s,%s,%s) RETURNING id""",
            (document_id, "legacy evidence", "c" * 64, "legacy", "1"),
        )
        chunk_id = cursor.fetchone()[0]
        manifest_ids = []
        for model_name, dimension, index_name in (
            ("legacy-a", 4, "legacy-a-index"),
            ("legacy-b", 8, "legacy-b-index"),
        ):
            cursor.execute(
                """INSERT INTO embedding_manifests(
                    chunk_id,model_name,dimension,distance_metric,index_status
                ) VALUES (%s,%s,%s,'Cosine','complete') RETURNING id""",
                (chunk_id, model_name, dimension),
            )
            manifest_ids.append(cursor.fetchone()[0])
            cursor.execute(
                """INSERT INTO index_jobs(
                    entity_type,entity_id,index_name,operation,status
                ) VALUES ('chunk',%s,%s,'upsert','complete')""",
                (chunk_id, index_name),
            )

    assert migrate(TEST_DSN, "0005_run_lifecycle") == 5
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO research_runs(
            original_request,query_plan,skill_version,retrieval_policy_version,
            status,external_run_id)
            VALUES('populated v5 run','{}','v5','v5','running',%s) RETURNING id""",
            (f"fr_v5_{uuid4().hex}",),
        )
        legacy_run_id = cursor.fetchone()[0]
        cursor.execute(
            """INSERT INTO research_runs(
            original_request,query_plan,skill_version,retrieval_policy_version,
            status,outcome,completed_at,external_run_id)
            VALUES('populated partial v5 run','{}','v5','v5','complete','partial',
              now(),%s) RETURNING id""",
            (f"fr_v5_partial_{uuid4().hex}",),
        )
        legacy_partial_run_id = cursor.fetchone()[0]

    # PostgreSQL transactional DDL leaves no partial workflow objects after an
    # interrupted attempt, so the supported repair is a normal forward retry.
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute("CREATE TABLE interrupted_v6_probe(id integer)")
            raise RuntimeError("synthetic interruption")
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('interrupted_v6_probe')")
        assert cursor.fetchone()[0] is None

    assert migrate(TEST_DSN) == 8
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT state,lifecycle_revision,execution_mode,objective
            FROM research_runs WHERE id=%s""",
            (legacy_run_id,),
        )
        assert cursor.fetchone() == ("created", 0, "legacy", "populated v5 run")
        cursor.execute(
            "SELECT state,declared_outcome FROM research_runs WHERE id=%s",
            (legacy_partial_run_id,),
        )
        assert cursor.fetchone() == ("partial", "partial")
        cursor.execute(
            """SELECT count(*),count(DISTINCT index_definition_id)
            FROM embedding_manifests WHERE chunk_id=%s""",
            (chunk_id,),
        )
        assert cursor.fetchone() == (2, 2)
        cursor.execute(
            """SELECT count(*),count(DISTINCT manifest_id),count(DISTINCT index_definition_id)
            FROM index_jobs WHERE manifest_id=ANY(%s)""",
            (manifest_ids,),
        )
        assert cursor.fetchone() == (2, 2, 2)
        cursor.execute(
            """INSERT INTO index_definitions(
                fingerprint,physical_collection,model_name,model_revision,
                dimension,distance_metric,normalization,instruction_template_hash
            ) VALUES (%s,%s,'legacy-a','',16,'Cosine','unit-length','')
            RETURNING id""",
            ("d" * 64, "legacy-dimension-variant"),
        )
        definition_id = cursor.fetchone()[0]
        cursor.execute(
            """INSERT INTO embedding_manifests(
                chunk_id,model_name,model_revision,dimension,distance_metric,
                normalization,instruction_template_hash,index_status,index_definition_id
            ) VALUES (%s,'legacy-a','',16,'Cosine','unit-length','','pending',%s)""",
            (chunk_id, definition_id),
        )
        cursor.execute(
            """SELECT conname FROM pg_constraint
            WHERE conrelid='embedding_manifests'::regclass AND contype='u'
            ORDER BY conname"""
        )
        assert [row[0] for row in cursor.fetchall()] == [
            "embedding_manifests_definition_key",
            "embedding_manifests_id_definition_key",
        ]


def test_workflow_repository_records_are_idempotent_and_referential(service):
    external_id = f"fr_workflow_{uuid4().hex}"
    with service.uow_factory() as uow:
        run_id = uow.start_run(
            "workflow schema",
            {"external_run_id": external_id, "execution_mode": "agent_led"},
        )
        external_invocation_id = f"fc_{uuid4().hex}"
        invocation_id = uow.record_invocation(
            run_id,
            "search",
            "invocation:create",
            external_invocation_id=external_invocation_id,
        )
        assert invocation_id == uow.record_invocation(
            run_id,
            "search",
            "invocation:create",
            external_invocation_id=external_invocation_id,
        )
        event_id = uow.append_event(
            run_id,
            "workflow.created",
            "system",
            "event:created",
            invocation_id=invocation_id,
            payload={"source": "integration"},
        )
        assert event_id == uow.append_event(
            run_id,
            "workflow.created",
            "system",
            "event:created",
            invocation_id=invocation_id,
            payload={"source": "integration"},
        )
        spec_id = uow.record_research_spec(
            run_id,
            1,
            "research-spec",
            1,
            {"schema_version": 1, "objective": "workflow schema"},
            "spec:v1",
        )
        budget_payload = {
            "snapshot_version": 1,
            "policy_version": "budget-policy-v1",
            "policy_config_sha256": "b" * 64,
            "spec_revision": 1,
            "run_revision": 0,
            "effective_caps": {"max_search_branches": 3},
        }
        budget_id = uow.record_budget_snapshot(
            run_id,
            spec_id,
            1,
            0,
            "budget-policy-v1",
            "b" * 64,
            budget_payload,
            "budget:v1:r0",
        )
        assert budget_id == uow.record_budget_snapshot(
            run_id,
            spec_id,
            1,
            0,
            "budget-policy-v1",
            "b" * 64,
            budget_payload,
            "budget:v1:r0",
        )
        call_id = uow.record_semantic_call(
            run_id,
            "planning",
            "host-agent",
            "host",
            "planning-v1",
            {"spec_id": str(spec_id)},
            "semantic-call:planning",
            invocation_id=invocation_id,
        )
        artifact_id = uow.record_semantic_artifact(
            run_id,
            call_id,
            "research_spec",
            "research-spec",
            1,
            {"spec_id": str(spec_id)},
            "semantic-artifact:spec",
        )
        export_id = uow.record_compatibility_export(
            run_id,
            "_meta.json",
            5,
            "a" * 64,
            "complete",
            "export:meta:v5",
            invocation_id=invocation_id,
            database_revision=0,
        )

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT r.research_spec_id,r.budget_snapshot_id,r.budget_policy_version,
            count(DISTINCT i.id),count(DISTINCT e.id),
            count(DISTINCT c.id),count(DISTINCT a.id),count(DISTINCT x.id)
            FROM research_runs r
            LEFT JOIN research_invocations i ON i.run_id=r.id
            LEFT JOIN research_events e ON e.run_id=r.id
            LEFT JOIN semantic_calls c ON c.run_id=r.id
            LEFT JOIN semantic_artifacts a ON a.run_id=r.id
            LEFT JOIN compatibility_exports x ON x.run_id=r.id
            WHERE r.id=%s GROUP BY r.research_spec_id,r.budget_snapshot_id,
            r.budget_policy_version""",
            (run_id,),
        )
        assert cursor.fetchone() == (
            spec_id,
            budget_id,
            "budget-policy-v1",
            1,
            1,
            1,
            1,
            1,
        )
        assert artifact_id is not None and export_id is not None


def test_shadow_comparisons_are_queryable_append_only_and_do_not_mutate_run(service):
    runs = ResearchRunService(service.uow_factory)
    created = runs.create(
        "shadow adapter",
        f"fr_shadow_{uuid4().hex}",
        execution_mode="agent_led",
    )
    adapter = LegacyEntryPointAdapter(service.uow_factory, AdapterMode.SHADOW)
    decision = {
        "action": "search",
        "status": "complete",
        "input": {"query": "adapter comparison"},
    }
    external_invocation_id = f"fc_{uuid4().hex}"
    first = adapter.route(
        "fsearch",
        decision,
        service_proposal={"action": "retrieve"},
        external_run_id=created.external_id,
        external_invocation_id=external_invocation_id,
        idempotency_key="shadow:comparison:one",
    )
    replay = adapter.route(
        "fsearch",
        decision,
        service_proposal={"action": "retrieve"},
        external_run_id=created.external_id,
        external_invocation_id=external_invocation_id,
        idempotency_key="shadow:comparison:one",
    )
    assert replay.comparison_id == first.comparison_id
    status = runs.status(run_id=created.id)
    assert status.state == "created"
    assert status.lifecycle_revision == 0
    with service.uow_factory() as uow:
        rows = uow.runs.list_legacy_adapter_comparisons(
            external_run_id=created.external_id
        )
    assert len(rows) == 1
    assert rows[0]["adapter_mode"] == "shadow"
    assert rows[0]["workflow_revision"] == 0
    assert rows[0]["divergent"] is True
    with service.uow_factory() as uow:
        divergent = uow.runs.list_legacy_adapter_comparisons(
            external_run_id=created.external_id, divergent_only=True
        )
    assert [row["id"] for row in divergent] == [first.comparison_id]
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        with pytest.raises(Exception, match="append-only"):
            cursor.execute(
                "UPDATE legacy_adapter_comparisons SET divergent=true WHERE id=%s",
                (first.comparison_id,),
            )


def test_budget_snapshot_changes_require_policy_or_run_revision(service):
    with service.uow_factory() as uow:
        run_id = uow.start_run(
            "budget revision", {"external_run_id": f"fr_budget_{uuid4().hex}"}
        )
        spec_id = uow.record_research_spec(
            run_id,
            1,
            "research-spec",
            1,
            {"schema_version": 1, "objective": "budget revision"},
            "spec:v1",
        )
        first = uow.record_budget_snapshot(
            run_id,
            spec_id,
            1,
            0,
            "budget-policy-v1",
            "c" * 64,
            {
                "policy_version": "budget-policy-v1",
                "policy_config_sha256": "c" * 64,
                "spec_revision": 1,
                "run_revision": 0,
                "effective_caps": {"max_search_branches": 2},
            },
            "budget:first",
        )
        assert first is not None
        with pytest.raises(
            ValueError,
            match="new policy version or explicit run revision",
        ):
            uow.record_budget_snapshot(
                run_id,
                spec_id,
                1,
                0,
                "budget-policy-v1",
                "c" * 64,
                {
                    "policy_version": "budget-policy-v1",
                    "policy_config_sha256": "c" * 64,
                    "spec_revision": 1,
                    "run_revision": 0,
                    "effective_caps": {"max_search_branches": 3},
                },
                "budget:changed",
            )
        second = uow.record_budget_snapshot(
            run_id,
            spec_id,
            1,
            0,
            "budget-policy-v2",
            "d" * 64,
            {
                "policy_version": "budget-policy-v2",
                "policy_config_sha256": "d" * 64,
                "spec_revision": 1,
                "run_revision": 0,
                "effective_caps": {"max_search_branches": 3},
            },
            "budget:v2",
        )
        assert second != first


def test_concurrent_event_idempotency_and_conflicting_reuse_rejection(service):
    with service.uow_factory() as uow:
        run_id = uow.start_run(
            "concurrent events",
            {"external_run_id": f"fr_event_{uuid4().hex}"},
        )

    def append_once(_attempt):
        with service.uow_factory() as uow:
            return uow.append_event(
                run_id,
                "workflow.created",
                "system",
                "event:created",
                payload={"source": "concurrent-test"},
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(append_once, range(2)))
    assert first == second

    with service.uow_factory() as uow:
        with pytest.raises(ValueError, match="another event"):
            uow.append_event(
                run_id,
                "workflow.changed",
                "system",
                "event:created",
                payload={"source": "different-command"},
            )

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT r.state,r.lifecycle_revision,count(e.id)
            FROM research_runs r LEFT JOIN research_events e ON e.run_id=r.id
            WHERE r.id=%s GROUP BY r.state,r.lifecycle_revision""",
            (run_id,),
        )
        assert cursor.fetchone() == ("created", 0, 1)


def test_transition_and_event_ledgers_are_append_only(service):
    with service.uow_factory() as uow:
        run_id = uow.start_run(
            "append only", {"external_run_id": f"fr_append_{uuid4().hex}"}
        )
        event_id = uow.append_event(
            run_id, "workflow.created", "system", "event:append-only"
        )
        transition_id = uow.append_run_transition(
            run_id,
            1,
            "created",
            "planning",
            "transition:append-only",
            "system",
            "state-policy-v1",
            triggering_event_id=event_id,
        )["id"]
        with pytest.raises(ValueError, match="another transition"):
            uow.append_run_transition(
                run_id,
                1,
                "created",
                "planning",
                "transition:append-only",
                "different-actor",
                "state-policy-v1",
                triggering_event_id=event_id,
            )

    for table, row_id in (
        ("research_events", event_id),
        ("research_run_transitions", transition_id),
    ):
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            with pytest.raises(Exception) as error:
                cursor.execute(f"DELETE FROM {table} WHERE id=%s", (row_id,))
            assert "append-only" in str(error.value)


def test_research_run_service_records_exactly_one_event_per_transition(service):
    runs = ResearchRunService(service.uow_factory)
    external_id = f"fr_service_{uuid4().hex}"
    created = runs.create("transactional state machine", external_id)
    commands = (
        ("planning", "transition:planning"),
        ("corpus_review", "transition:corpus-review"),
        ("retrieving", "transition:retrieving"),
        ("synthesizing", "transition:synthesizing"),
        ("validating", "transition:validating"),
        ("completed", "transition:completed"),
    )
    revision = 0
    for state, key in commands:
        result = runs.transition(
            created.id,
            state,
            expected_revision=revision,
            idempotency_key=key,
            actor_type="integration-test",
            outcome="satisfied" if state == "completed" else None,
        )
        revision += 1
        assert result.lifecycle_revision == revision
        assert result.next_state == state
        assert not result.reused

    status = runs.status(run_id=created.id)
    assert status.state == "completed"
    assert status.lifecycle_revision == len(commands)
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT count(*),count(DISTINCT triggering_event_id)
            FROM research_run_transitions WHERE run_id=%s""",
            (created.id,),
        )
        assert cursor.fetchone() == (len(commands), len(commands))
        cursor.execute(
            """SELECT count(*) FROM research_events
            WHERE run_id=%s AND event_type <> 'run.created'""",
            (created.id,),
        )
        assert cursor.fetchone()[0] == len(commands)


def test_research_run_service_idempotent_retry_and_conflicting_reuse(service):
    runs = ResearchRunService(service.uow_factory)
    created = runs.create("idempotent transition", f"fr_idempotent_{uuid4().hex}")
    command = {
        "expected_revision": 0,
        "idempotency_key": "transition:planning",
        "actor_type": "integration-test",
    }
    first = runs.transition(created.id, "planning", **command)
    second = runs.transition(created.id, "planning", **command)
    assert second.transition_id == first.transition_id
    assert second.event_id == first.event_id
    assert second.reused

    with pytest.raises(ValueError, match="another run command"):
        runs.transition(
            created.id,
            "planning",
            expected_revision=0,
            idempotency_key="transition:planning",
            actor_type="different-actor",
        )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT count(t.id),count(e.id)
            FROM research_runs r
            LEFT JOIN research_run_transitions t ON t.run_id=r.id
            LEFT JOIN research_events e ON e.run_id=r.id
              AND e.event_type <> 'run.created'
            WHERE r.id=%s""",
            (created.id,),
        )
        assert cursor.fetchone() == (1, 1)


def test_research_run_service_serializes_conflicting_transitions(service):
    runs = ResearchRunService(service.uow_factory)
    created = runs.create(
        "concurrent transition", f"fr_concurrent_transition_{uuid4().hex}"
    )
    runs.transition(
        created.id,
        "planning",
        expected_revision=0,
        idempotency_key="transition:planning",
        actor_type="integration-test",
    )

    def transition(candidate):
        try:
            return runs.transition(
                created.id,
                candidate,
                expected_revision=1,
                idempotency_key=f"transition:{candidate}",
                actor_type="integration-test",
            )
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(transition, ("corpus_review", "failed")))
    assert sum(not isinstance(result, Exception) for result in results) == 1
    rejected = next(result for result in results if isinstance(result, Exception))
    assert isinstance(rejected, StaleRunRevisionError)

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT lifecycle_revision,count(*) FROM research_run_transitions
            WHERE run_id=%s GROUP BY lifecycle_revision ORDER BY lifecycle_revision""",
            (created.id,),
        )
        assert cursor.fetchall() == [(1, 1), (2, 1)]


def test_reopen_increments_revision_and_invalidates_semantic_artifacts(service):
    runs = ResearchRunService(service.uow_factory)
    created = runs.create("reopen semantics", f"fr_reopen_service_{uuid4().hex}")
    with service.uow_factory() as uow:
        call_id = uow.record_semantic_call(
            created.id,
            "planning",
            "host-agent",
            "host",
            "planning-v1",
            {"proposal": "planning"},
            "semantic-call:planning",
        )
        artifact_id = uow.record_semantic_artifact(
            created.id,
            call_id,
            "state_transition",
            "transition-proposal",
            1,
            {"next_state": "planning", "run_revision": 0},
            "semantic-artifact:planning",
        )
    runs.transition(
        created.id,
        "planning",
        expected_revision=0,
        idempotency_key="transition:planning",
        actor_type="host-agent",
        semantic_proposal_id=artifact_id,
    )
    with pytest.raises(ValueError, match="stale"):
        runs.transition(
            created.id,
            "corpus_review",
            expected_revision=1,
            idempotency_key="transition:stale-before-reopen",
            actor_type="host-agent",
            semantic_proposal_id=artifact_id,
        )
    runs.fail(
        created.id,
        expected_revision=1,
        idempotency_key="transition:failed",
        actor_type="system",
        error="synthetic failure",
    )
    reopened = runs.reopen(
        created.id,
        expected_revision=2,
        idempotency_key="transition:reopen",
        actor_type="operator",
        reason="new evidence",
    )
    assert reopened.lifecycle_revision == 3
    status = runs.status(run_id=created.id)
    assert status.state == "created"
    assert status.reopened_from_revision == 2

    with pytest.raises(ValueError, match="stale"):
        runs.transition(
            created.id,
            "planning",
            expected_revision=3,
            idempotency_key="transition:stale-proposal",
            actor_type="host-agent",
            semantic_proposal_id=artifact_id,
        )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT validation_status,validation_errors
            FROM semantic_artifacts WHERE id=%s""",
            (artifact_id,),
        )
        validation_status, errors = cursor.fetchone()
        assert validation_status == "invalid"
        assert errors[-1]["code"] == "stale_after_reopen"
        assert errors[-1]["invalidated_by_revision"] == 3


def test_semantic_call_service_retains_failures_and_host_provenance(service):
    runs = ResearchRunService(service.uow_factory)
    semantic = SemanticCallService(service.uow_factory)
    created = runs.create(
        "semantic persistence",
        f"fr_semantic_{uuid4().hex}",
        execution_mode="autonomous_local",
    )
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {"result": {"type": "string"}}, "required": ["result"],
    }
    failed_context = {
        "run_id": created.id, "stage": "planning", "schema_name": "test-result",
        "schema_version": 1, "artifact_type": "test_result",
        "run_revision": 0,
        "idempotency_key": "semantic:model:timeout",
    }
    call_id = semantic.start_model_call(
        failed_context, provider="local", requested_model="chat", model_revision="rev-1",
        endpoint_alias="local", prompt_version="test-v1", prompt_hash="a" * 64,
        schema=schema, input_token_estimate=12,
    )
    semantic.finish_model_call(
        failed_context, call_id, status="failed",
        provenance={"provider": "local", "requested_model": "chat"},
        attempts=[{"attempt": 1, "latency_ms": 50, "error": "TimeoutError"}],
        artifacts=[], error="TimeoutError",
    )
    failed = semantic.inspect(created.id, call_id)
    assert failed["status"] == "failed"
    assert failed["response_metadata"]["attempts"][0]["error"] == "TimeoutError"
    assert failed["artifacts"] == []

    host_run = runs.create(
        "host semantic persistence",
        f"fr_host_semantic_{uuid4().hex}",
        execution_mode="agent_led",
    )
    host_context = {
        **failed_context,
        "run_id": host_run.id,
        "idempotency_key": "semantic:host:accepted",
        "input_artifact_ids": [uuid4()],
    }
    accepted = semantic.ingest_host_artifact(
        host_context, {"result": "accepted"}, schema, actor_identifier="codex"
    )
    stored = semantic.inspect(host_run.id, UUID(accepted.provenance["semantic_call_id"]))
    assert stored["provider"] == "host-agent"
    assert stored["model"] == ""
    assert stored["request"]["authority"] == "host-agent"
    assert "endpoint_alias" not in stored["request"]
    assert stored["response_metadata"]["transport_attempts"] == []
    assert stored["artifacts"][0]["validation_status"] == "valid"


def test_explicit_mode_change_records_approval_and_invalidates_prior_authority(service):
    runs = ResearchRunService(service.uow_factory)
    semantic = SemanticCallService(service.uow_factory)
    created = runs.create(
        "mode revision",
        f"fr_mode_revision_{uuid4().hex}",
        execution_mode="agent_led",
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
    }
    context = {
        "run_id": created.id,
        "run_revision": 0,
        "stage": "planning",
        "schema_name": "test-result",
        "schema_version": 1,
        "artifact_type": "test_result",
        "idempotency_key": "semantic:host:before-mode-change",
    }
    supplied = semantic.ingest_host_artifact(
        context, {"result": "host plan"}, schema, actor_identifier="codex"
    )

    command = {
        "expected_revision": 0,
        "idempotency_key": "mode-change:autonomous",
        "requested_by": "operator-a",
        "approved_by": "operator-b",
        "reason": "switch to unattended local execution",
        "actor_type": "operator",
        "actor_identifier": "operator-b",
    }
    changed = runs.change_execution_mode(
        created.id, "autonomous_local", **command
    )
    replay = runs.change_execution_mode(
        created.id, "autonomous_local", **command
    )
    assert replay.event_id == changed.event_id
    assert replay.reused is True
    assert changed.lifecycle_revision == 1
    status = runs.status(run_id=created.id)
    assert status.execution_mode == "autonomous_local"
    assert status.lifecycle_revision == 1

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT actor_type,actor_identifier,payload,run_revision
            FROM research_events WHERE id=%s""",
            (changed.event_id,),
        )
        actor_type, actor_identifier, payload, run_revision = cursor.fetchone()
        assert actor_type == "operator"
        assert actor_identifier == "operator-b"
        assert payload["requested_by"] == "operator-a"
        assert payload["approved_by"] == "operator-b"
        assert payload["prior_mode"] == "agent_led"
        assert payload["next_mode"] == "autonomous_local"
        assert run_revision == 1
        cursor.execute(
            """SELECT validation_status,validation_errors
            FROM semantic_artifacts WHERE id=%s""",
            (UUID(supplied.provenance["semantic_artifact_id"]),),
        )
        validation_status, errors = cursor.fetchone()
        assert validation_status == "invalid"
        assert errors[-1]["code"] == "stale_after_mode_change"

    with pytest.raises(StaleRunRevisionError):
        runs.change_execution_mode(
            created.id,
            "deterministic_debug",
            expected_revision=0,
            idempotency_key="mode-change:stale",
            requested_by="operator-a",
            approved_by="operator-b",
            reason="stale proposal",
        )
    with pytest.raises(ValueError, match="mode-change approver is required"):
        runs.change_execution_mode(
            created.id,
            "deterministic_debug",
            expected_revision=1,
            idempotency_key="mode-change:unapproved",
            requested_by="operator-a",
            approved_by="",
            reason="missing approval",
        )

    with pytest.raises(ValueError, match="requires local-model"):
        semantic.ingest_host_artifact(
            {**context, "run_revision": 1, "idempotency_key": "semantic:host:stale-authority"},
            {"result": "should be rejected"},
            schema,
        )


def test_stale_revision_terminal_mutation_and_cancel_fail_closed(service):
    runs = ResearchRunService(service.uow_factory)
    created = runs.create("failure paths", f"fr_failure_paths_{uuid4().hex}")
    runs.transition(
        created.id,
        "planning",
        expected_revision=0,
        idempotency_key="transition:planning",
        actor_type="integration-test",
    )
    with pytest.raises(StaleRunRevisionError):
        runs.transition(
            created.id,
            "corpus_review",
            expected_revision=0,
            idempotency_key="transition:stale",
            actor_type="integration-test",
        )
    runs.fail(
        created.id,
        expected_revision=1,
        idempotency_key="transition:failed",
        actor_type="integration-test",
        error="synthetic failure",
    )
    with pytest.raises(RunStateError, match="not permitted"):
        runs.transition(
            created.id,
            "corpus_review",
            expected_revision=2,
            idempotency_key="transition:terminal-mutation",
            actor_type="integration-test",
        )

    other = runs.create("cancel path", f"fr_cancel_{uuid4().hex}")
    cancelled = runs.cancel(
        other.id,
        expected_revision=0,
        idempotency_key="transition:cancel",
        actor_type="operator",
        reason="operator request",
    )
    assert cancelled.next_state == "cancelled"
    assert runs.status(run_id=other.id).state == "cancelled"


def test_run_cli_exposes_machine_readable_status_and_transitions(monkeypatch, capsys):
    external_id = f"fr_cli_state_{uuid4().hex}"
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    assert (
        store_cli.main(
            [
                "run-start",
                external_id,
                "CLI state representation",
                "--mode",
                "agent_led",
            ]
        )
        == 0
    )
    started = json.loads(capsys.readouterr().out)
    assert started["state"] == "created"
    assert started["lifecycle_revision"] == 0
    assert started["terminal"] is False

    assert store_cli.main(["run-status", external_id]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["id"] == started["id"]
    assert status["state"] == "created"

    assert (
        store_cli.main(
            [
                "run-mode-change",
                external_id,
                "autonomous_local",
                "--expected-revision",
                "0",
                "--idempotency-key",
                "cli:mode-change",
                "--requested-by",
                "cli-user",
                "--approved-by",
                "cli-approver",
                "--reason",
                "exercise explicit CLI mode revision",
            ]
        )
        == 0
    )
    mode_changed = json.loads(capsys.readouterr().out)
    assert mode_changed["prior_mode"] == "agent_led"
    assert mode_changed["next_mode"] == "autonomous_local"
    assert mode_changed["lifecycle_revision"] == 1

    assert (
        store_cli.main(
            [
                "run-transition",
                external_id,
                "planning",
                "--expected-revision",
                "1",
                "--idempotency-key",
                "cli:planning",
            ]
        )
        == 0
    )
    transitioned = json.loads(capsys.readouterr().out)
    assert transitioned["next_state"] == "planning"
    assert transitioned["lifecycle_revision"] == 2

    assert (
        store_cli.main(
            [
                "run-cancel",
                external_id,
                "--expected-revision",
                "2",
                "--idempotency-key",
                "cli:cancel",
                "--reason",
                "integration test",
            ]
        )
        == 0
    )
    cancelled = json.loads(capsys.readouterr().out)
    assert cancelled["next_state"] == "cancelled"


def test_workflow_constraints_reject_cross_run_and_invalid_hash(service):
    with service.uow_factory() as uow:
        first_run = uow.start_run(
            "first", {"external_run_id": f"fr_first_{uuid4().hex}"}
        )
        first_invocation = uow.record_invocation(
            first_run, "search", "first:invocation"
        )
    with service.uow_factory() as uow:
        second_run = uow.start_run(
            "second", {"external_run_id": f"fr_second_{uuid4().hex}"}
        )

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        with pytest.raises(Exception) as cross_run:
            cursor.execute(
                """INSERT INTO research_events(
                run_id,invocation_id,event_type,actor_type,run_revision,idempotency_key)
                VALUES(%s,%s,'invalid.cross_run','test',0,'cross-run')""",
                (second_run, first_invocation),
            )
        assert "research_events_invocation_id_run_id_fkey" in str(cross_run.value)

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        with pytest.raises(Exception) as bad_hash:
            cursor.execute(
                """INSERT INTO research_specs(
                run_id,spec_revision,schema_name,schema_version,payload,content_sha256,
                validation_status,idempotency_key)
                VALUES(%s,1,'research-spec',1,'{}','not-a-hash','valid','bad-hash')""",
                (second_run,),
            )
        assert "research_specs_content_sha256_check" in str(bad_hash.value)


def test_firecrawl_result_versioning_and_transactional_index_jobs(service):
    url = f"https://integration.example/{uuid4()}"
    first = service.ingest(
        IngestRequest(
            url, b"# V1\n\nRaw first.", normalized_content=b"# V1\n\nNormalized first."
        )
    )
    unchanged = service.ingest(
        IngestRequest(
            url, b"# V1\n\nRaw first.", normalized_content=b"# V1\n\nNormalized first."
        )
    )
    changed = service.ingest(IngestRequest(url, b"# V2\n\nRaw changed."))
    assert unchanged.reused_snapshot and unchanged.snapshot_id == first.snapshot_id
    assert changed.snapshot_id != first.snapshot_id
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT parent_snapshot_id FROM asset_snapshots WHERE id=%s",
            (changed.snapshot_id,),
        )
        assert cursor.fetchone()[0] == first.snapshot_id
        cursor.execute(
            "SELECT count(*) FROM index_jobs WHERE entity_id=ANY(%s)",
            (list(changed.chunk_ids),),
        )
        assert cursor.fetchone()[0] == len(changed.chunk_ids)


def test_bounded_targeted_passage_retrieval(service):
    result = service.ingest(
        IngestRequest(
            f"https://integration.example/{uuid4()}",
            b"# Evidence\n\nCitation-ready text.",
        )
    )
    passages = service.fetch_passages(
        list(result.chunk_ids), max_tokens=100, max_passages=1
    )
    assert len(passages) == 1
    assert passages[0]["snapshot_id"] == result.snapshot_id
    assert passages[0]["source_id"] == result.source_id


def test_concurrent_same_source_ingest_has_stable_identity(service):
    url = f"https://integration.example/concurrent/{uuid4()}"
    request = IngestRequest(url, b"# Concurrent identity\n\nStable corpus object.")
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(service.ingest, (request, request)))

    assert first.snapshot_id == second.snapshot_id
    assert first.document_id == second.document_id
    assert first.chunk_ids == second.chunk_ids
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT count(DISTINCT a.id),count(DISTINCT d.id),count(DISTINCT c.id)
            FROM sources s JOIN asset_snapshots a ON a.source_id=s.id
            JOIN documents d ON d.snapshot_id=a.id
            JOIN chunks c ON c.document_id=d.id WHERE s.canonical_url=%s""",
            (url,),
        )
        assert cursor.fetchone() == (1, 1, len(first.chunk_ids))


def test_lexical_search_selects_only_configured_derivation(service):
    marker = f"derivationmarker{uuid4().hex}"
    url = f"https://integration.example/derivation/{uuid4()}"
    request = IngestRequest(
        url, f"# Derivation\n\n{marker} retained evidence.".encode()
    )
    active = service.ingest(request)
    alternate_config = replace(
        service.config,
        parser_version="markdown-integration-alternate",
        normalization_version="cleanup-integration-alternate",
        chunker_version="structural-integration-alternate",
    )
    alternate_service = build_service(alternate_config)
    alternate = alternate_service.ingest(request)

    assert alternate.snapshot_id == active.snapshot_id
    assert alternate.document_id != active.document_id
    assert alternate.chunk_ids != active.chunk_ids
    with service.uow_factory() as uow:
        active_hits = uow.search_lexical(marker, 10, {})
    with alternate_service.uow_factory() as uow:
        alternate_hits = uow.search_lexical(marker, 10, {})
    assert {row["candidate_id"] for row in active_hits} == set(active.chunk_ids)
    assert {row["candidate_id"] for row in alternate_hits} == set(alternate.chunk_ids)


def test_batch_records_acquisition_failure_without_losing_success(service):
    invocation_id = f"fc_integration_{uuid4().hex}"
    good_url = f"https://integration.example/batch/{uuid4()}"
    manifest = service.ingest_batch(
        invocation_id,
        "integration",
        [
            IngestRequest(good_url, b"# Good\n\nCommitted through the outer batch."),
            {
                "requested_url": "https://integration.example/unreachable",
                "error": "synthetic acquisition failure",
            },
        ],
    )

    assert manifest["status"] == "partial"
    assert manifest["failure_count"] == 1
    assert [asset["status"] for asset in manifest["assets"]] == [
        "complete",
        "failed",
    ]
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM sources WHERE canonical_url=%s", (good_url,)
        )
        assert cursor.fetchone()[0] == 1

    replacement = service.ingest_batch(
        invocation_id,
        "integration",
        [
            {
                "request": IngestRequest(
                    good_url, b"# Retry\n\nReplacement invocation ledger."
                ),
                "metadata": {"firecrawl": {"result_index": 7}},
            }
        ],
    )
    assert replacement["status"] == "complete"
    assert [(item["ordinal"], item["status"]) for item in replacement["assets"]] == [
        (7, "complete")
    ]
    with pytest.raises(ValueError, match="original operation and research run"):
        service.ingest_batch(invocation_id, "different-operation", [])
    external_run = f"fr_integration_{uuid4().hex}"
    with service.uow_factory() as uow:
        uow.start_run("other owner", {"external_run_id": external_run})
    with pytest.raises(ValueError, match="original operation and research run"):
        service.ingest_batch(
            invocation_id,
            "integration",
            [],
            research_run_external_id=external_run,
        )


def test_batch_rejects_invalid_run_and_active_reuse_before_ledger_mutation(service):
    missing_run = f"fr_missing_{uuid4().hex}"
    missing_invocation = f"fc_missing_{uuid4().hex}"
    with pytest.raises(KeyError, match=missing_run):
        service.ingest_batch(
            missing_invocation,
            "integration",
            [],
            research_run_external_id=missing_run,
        )

    finished_run = f"fr_finished_{uuid4().hex}"
    finished_invocation = f"fc_finished_{uuid4().hex}"
    with service.uow_factory() as uow:
        run_id = uow.start_run("finished owner", {"external_run_id": finished_run})
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE research_runs SET status='complete',outcome='test-complete',
            completed_at=now() WHERE id=%s""",
            (run_id,),
        )
    with pytest.raises(ValueError, match="require a running research run"):
        service.ingest_batch(
            finished_invocation,
            "integration",
            [],
            research_run_external_id=finished_run,
        )

    active_invocation = f"fc_active_{uuid4().hex}"
    with service.uow_factory() as uow:
        uow.start_ingestion_batch(active_invocation, "integration")
    with pytest.raises(ValueError, match="already running"):
        service.ingest_batch(active_invocation, "integration", [])

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT invocation_id FROM ingestion_batches
            WHERE invocation_id=ANY(%s) ORDER BY invocation_id""",
            ([missing_invocation, finished_invocation, active_invocation],),
        )
        assert cursor.fetchall() == [(active_invocation,)]


def test_finished_run_is_immutable_and_rejects_new_evidence(service):
    external_id = f"fr_integration_{uuid4().hex}"
    asset = service.ingest(
        IngestRequest(
            f"https://integration.example/run/{uuid4()}",
            b"# Run evidence\n\nImmutable after finish.",
        )
    )
    with service.uow_factory() as uow:
        run_id = uow.start_run("original request", {"external_run_id": external_id})
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE research_runs SET status='complete',outcome='test-complete',
            completed_at=now() WHERE id=%s""",
            (run_id,),
        )
    with service.uow_factory() as uow:
        with pytest.raises(ValueError, match="another run"):
            uow.start_run("mutated request", {"external_run_id": external_id})
        with pytest.raises(KeyError):
            uow.link_run_asset(external_id, asset.snapshot_id)
        with pytest.raises(KeyError):
            uow.log_retrieval(
                run_id,
                {
                    "stage": "retriever",
                    "query": "late evidence",
                    "retriever": "lexical",
                    "candidate_type": "chunk",
                    "candidate_id": asset.chunk_ids[0],
                    "rank": 1,
                },
            )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT original_request,status FROM research_runs WHERE id=%s", (run_id,)
        )
        assert cursor.fetchone() == ("original request", "complete")


def test_finish_reopen_refinish_clears_completion_state(service, monkeypatch):
    external_id = f"fr_reopen_{uuid4().hex}"
    with service.uow_factory() as uow:
        run_id = uow.start_run("reopen lifecycle", {"external_run_id": external_id})
    runs = ResearchRunService(service.uow_factory)

    def advance_to_validating(start_revision):
        revision = start_revision
        for state in (
            "planning",
            "corpus_review",
            "retrieving",
            "synthesizing",
            "validating",
        ):
            runs.transition(
                run_id,
                state,
                expected_revision=revision,
                idempotency_key=f"advance:{start_revision}:{state}",
                actor_type="integration-test",
            )
            revision += 1
        return revision

    first_terminal_revision = advance_to_validating(0)
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    assert (
        store_cli.main(
            [
                "run-finish",
                external_id,
                "--outcome",
                "satisfied",
                "--source-manifest-sha256",
                "a" * 64,
                "--answer-sha256",
                "b" * 64,
            ]
        )
        == 0
    )
    assert store_cli.main(["run-reopen", external_id]) == 0
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT status,outcome,completed_at,source_manifest_sha256,answer_sha256,
            state,lifecycle_revision,reopened_from_revision
            FROM research_runs WHERE id=%s""",
            (run_id,),
        )
        assert cursor.fetchone() == (
            "running",
            None,
            None,
            None,
            None,
            "created",
            first_terminal_revision + 2,
            first_terminal_revision + 1,
        )
        with pytest.raises(Exception) as error:
            cursor.execute(
                "UPDATE research_runs SET status='unexpected' WHERE id=%s", (run_id,)
            )
        assert "research_runs_lifecycle_check" in str(error.value)
    second_terminal_revision = advance_to_validating(first_terminal_revision + 2)
    assert store_cli.main(["run-finish", external_id, "--outcome", "satisfied"]) == 0
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT status,outcome,source_manifest_sha256,answer_sha256,
            state,lifecycle_revision
            FROM research_runs WHERE id=%s""",
            (run_id,),
        )
        assert cursor.fetchone() == (
            "complete",
            "satisfied",
            None,
            None,
            "completed",
            second_terminal_revision + 1,
        )


def test_expired_final_attempt_becomes_dead_and_manifest_failed(service):
    result = service.ingest(
        IngestRequest(
            f"https://integration.example/exhausted/{uuid4()}",
            b"# Exhausted lease\n\nMust never remain running forever.",
        )
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE index_jobs SET status='running',attempt_count=5,
            lease_token=gen_random_uuid(),lease_owner='crashed-worker',
            lease_expires_at=now()-interval '1 minute'
            WHERE entity_id=%s RETURNING id,manifest_id""",
            (result.chunk_ids[0],),
        )
        job_id, manifest_id = cursor.fetchone()
    with service.uow_factory() as uow:
        uow.claim_jobs(1, max_attempts=5)
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT j.status,j.error,m.index_status,m.error
            FROM index_jobs j JOIN embedding_manifests m ON m.id=j.manifest_id
            WHERE j.id=%s AND m.id=%s""",
            (job_id, manifest_id),
        )
        status, error, manifest_status, manifest_error = cursor.fetchone()
    assert (status, manifest_status) == ("dead", "failed")
    assert error == manifest_error == "lease expired after final allowed attempt"


def test_job_completion_requires_exact_lease_token(service):
    result = service.ingest(
        IngestRequest(
            f"https://integration.example/token/{uuid4()}",
            b"# Lease token\n\nOnly the owning worker may complete.",
        )
    )
    lease_token = uuid4()
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE index_jobs SET status='running',attempt_count=1,
            lease_token=%s,lease_owner='integration',lease_expires_at=now()+interval '5 minutes'
            WHERE entity_id=%s RETURNING id""",
            (lease_token, result.chunk_ids[0]),
        )
        job_id = cursor.fetchone()[0]
    with service.uow_factory() as uow:
        with pytest.raises(TypeError):
            uow.finish_job(job_id, None)
        assert uow.finish_job(job_id, uuid4()) is False
        assert uow.finish_job(job_id, lease_token) is True


def test_job_manifest_definition_mismatch_is_rejected(service):
    request = IngestRequest(
        f"https://integration.example/definition/{uuid4()}",
        b"# Definition binding\n\nA job cannot escape its manifest definition.",
    )
    first = service.ingest(request)
    alternate = build_service(
        replace(service.config, embedding_revision=f"alternate-{uuid4().hex}")
    )
    alternate.ingest(request)
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT m.id,m.index_definition_id
            FROM embedding_manifests m WHERE m.chunk_id=%s
            ORDER BY m.id LIMIT 1""",
            (first.chunk_ids[0],),
        )
        manifest_id, original_definition = cursor.fetchone()
        cursor.execute(
            """SELECT index_definition_id FROM embedding_manifests
            WHERE chunk_id=%s AND index_definition_id<>%s LIMIT 1""",
            (first.chunk_ids[0], original_definition),
        )
        other_definition = cursor.fetchone()[0]
        with pytest.raises(Exception) as error:
            cursor.execute(
                """UPDATE index_jobs SET index_definition_id=%s
                WHERE manifest_id=%s""",
                (other_definition, manifest_id),
            )
        assert "index_jobs_manifest_definition_fk" in str(error.value)
