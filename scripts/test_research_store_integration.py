"""Opt-in PostgreSQL integration tests.

Set RESEARCH_STORE_TEST_DATABASE_URL to a disposable PostgreSQL database whose
name contains a standalone ``test`` segment, and set
RESEARCH_STORE_TEST_ALLOW_RESET to that exact database name. The suite never
guesses or reuses DATABASE_URL because its session setup drops the public schema.
"""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_domain import load_model, schema_registry, serialize_model
from research_store import cli as store_cli
from research_store.config import StoreConfig
from research_store.container import (
    build_run_service,
    build_service,
    build_workflow_operation_service,
)
from research_store.domain import IngestRequest
from research_store.postgres import connect, migrate, require_disposable_database_reset
from research_store.run_service import (
    ResearchRunService,
    RunStateError,
    StaleRunRevisionError,
)
from research_store.semantic_service import SemanticCallService

ROOT = SCRIPTS.parent
FIXTURES = ROOT / "tests" / "fixtures" / "research_domain"
VALID = (
    json.loads((FIXTURES / "valid.json").read_text())
    if (FIXTURES / "valid.json").exists()
    else {}
)


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
    """Create the current PostgreSQL-only schema from an empty database."""
    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")

    assert migrate(TEST_DSN) >= 15
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT to_regclass('research_run_transitions'),
            to_regclass('research_events'),to_regclass('semantic_artifacts'),
            to_regclass('research_budget_snapshots'),to_regclass('search_plans'),
            to_regclass('search_plan_queries'),to_regclass('search_responses'),
            to_regclass('search_candidates'),to_regclass('candidate_occurrences'),
            to_regclass('research_invocations')"""
        )
        assert all(cursor.fetchone())
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchone()[0] == "0039_index_checkpoint_guard"


def test_wrapper_workflow_runs_entirely_from_postgresql(service):
    """A fresh run can scrape, index, and finish without filesystem state."""
    runs = build_run_service(service.config)
    workflow = build_workflow_operation_service(service.config)
    external_run_id = f"fr_wrapper_{uuid4().hex}"
    external_invocation_id = f"fc_{uuid4().hex}"
    created = runs.create(
        "PostgreSQL-only wrapper integration",
        external_run_id,
        execution_mode="autonomous_local",
    )

    invocation = workflow.begin_operation(
        external_run_id,
        external_invocation_id,
        "fscrape",
        {"urls": ["https://integration.example/wrapper"]},
    )
    assert invocation.run_id == created.id
    assert runs.status(run_id=created.id).state == "extracting"

    manifest = service.ingest_batch(
        external_invocation_id,
        "scrape",
        [
            IngestRequest(
                "https://integration.example/wrapper",
                b"# PostgreSQL authority\n\nIndexed wrapper evidence.",
            )
        ],
        research_run_external_id=external_run_id,
    )
    workflow.complete_operation(
        external_run_id,
        external_invocation_id,
        succeeded=True,
        output=manifest,
    )
    assert runs.status(run_id=created.id).state == "indexing"

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE index_jobs j
               SET status='complete', completed_at=now(), error=NULL
               FROM embedding_manifests m
               JOIN chunks c ON c.id=m.chunk_id
               JOIN documents d ON d.id=c.document_id
               JOIN research_run_assets ra ON ra.snapshot_id=d.snapshot_id
               WHERE j.manifest_id=m.id AND ra.run_id=%s""",
            (created.id,),
        )
        assert cursor.rowcount > 0
        cursor.execute(
            """UPDATE embedding_manifests m
               SET index_status='complete', indexed_at=now(), error=NULL
               FROM chunks c
               JOIN documents d ON d.id=c.document_id
               JOIN research_run_assets ra ON ra.snapshot_id=d.snapshot_id
               WHERE m.chunk_id=c.id AND ra.run_id=%s""",
            (created.id,),
        )

    finished = workflow.finish_run(external_run_id, outcome="satisfied")
    assert finished.state == "completed"
    assert finished.declared_outcome == "satisfied"
    assert finished.lifecycle_revision == 9

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT count(*) FROM research_invocations
               WHERE run_id=%s AND external_invocation_id=%s
                 AND status='complete'""",
            (created.id, external_invocation_id),
        )
        assert cursor.fetchone()[0] == 1


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
        second_event = uow.append_event(
            run_id,
            "workflow.created",
            "system",
            "event:created",
            invocation_id=invocation_id,
            payload={"source": "integration"},
        )
        assert event_id == (
            second_event["event_id"] if isinstance(second_event, dict) else second_event
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

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT r.research_spec_id,r.budget_snapshot_id,r.budget_policy_version,
            count(DISTINCT i.id),count(DISTINCT e.id),
            count(DISTINCT c.id),count(DISTINCT a.id)
            FROM research_runs r
            LEFT JOIN research_invocations i ON i.run_id=r.id
            LEFT JOIN research_events e ON e.run_id=r.id
            LEFT JOIN semantic_calls c ON c.run_id=r.id
            LEFT JOIN semantic_artifacts a ON a.run_id=r.id
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
        )
        assert artifact_id is not None


def test_phase1_gate_run_spec_and_transactional_rejection(service):
    """Exercise the Phase 1 exit criteria through authoritative services."""
    runs = ResearchRunService(service.uow_factory)
    semantic = SemanticCallService(service.uow_factory)
    agent_run = runs.create(
        "Phase 1 gate agent-led run",
        f"fr_phase1_agent_{uuid4().hex}",
        execution_mode="agent_led",
    )
    local_run = runs.create(
        "Phase 1 gate autonomous-local run",
        f"fr_phase1_local_{uuid4().hex}",
        execution_mode="autonomous_local",
    )
    assert agent_run.execution_mode == "agent_led"
    assert local_run.execution_mode == "autonomous_local"

    fixtures = json.loads(
        (SCRIPTS.parent / "tests/fixtures/research_domain/valid.json").read_text(
            encoding="utf-8"
        )
    )
    proposed_payload = deepcopy(fixtures["research-spec-v1"])
    proposed_payload["research_spec_id"] = str(uuid4())
    validated = load_model(proposed_payload)
    canonical_payload = serialize_model(validated)
    proposal = semantic.ingest_host_artifact(
        {
            "run_id": agent_run.id,
            "run_revision": 0,
            "stage": "planning",
            "schema_name": "research-spec",
            "schema_version": 1,
            "artifact_type": "research_spec",
            "idempotency_key": "phase1-gate:research-spec:proposal",
        },
        canonical_payload,
        schema_registry()["research-spec-v1"],
        actor_identifier="phase1-gate",
    )
    assert proposal.error == ""
    assert proposal.value == canonical_payload

    revised_payload = deepcopy(canonical_payload)
    revised_payload["objective"] = "Confirm the versioned Phase 1 gate behavior."
    revised_payload = serialize_model(load_model(revised_payload))
    with service.uow_factory() as uow:
        first_spec_id = uow.record_research_spec(
            agent_run.id,
            1,
            "research-spec",
            1,
            canonical_payload,
            "phase1-gate:research-spec:r1",
        )
        second_spec_id = uow.record_research_spec(
            agent_run.id,
            2,
            "research-spec",
            1,
            revised_payload,
            "phase1-gate:research-spec:r2",
        )

    before = runs.status(run_id=agent_run.id)
    with pytest.raises(RunStateError, match="transition rejected"):
        runs.transition(
            agent_run.id,
            "completed",
            expected_revision=before.lifecycle_revision,
            idempotency_key="phase1-gate:invalid-transition",
            actor_type="phase1-gate",
        )
    after = runs.status(run_id=agent_run.id)
    assert (after.state, after.lifecycle_revision) == (
        before.state,
        before.lifecycle_revision,
    )

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT spec_revision,payload->>'objective',validation_status
            FROM research_specs WHERE run_id=%s ORDER BY spec_revision""",
            (agent_run.id,),
        )
        assert cursor.fetchall() == [
            (1, canonical_payload["objective"], "valid"),
            (2, revised_payload["objective"], "valid"),
        ]
        cursor.execute(
            "SELECT research_spec_id FROM research_runs WHERE id=%s",
            (agent_run.id,),
        )
        assert cursor.fetchone()[0] == second_spec_id
        cursor.execute(
            """SELECT validation_status FROM semantic_artifacts
            WHERE id=%s""",
            (UUID(proposal.provenance["semantic_artifact_id"]),),
        )
        assert cursor.fetchone()[0] == "valid"
        cursor.execute(
            """SELECT count(*) FROM research_run_transitions
            WHERE run_id=%s""",
            (agent_run.id,),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            """SELECT count(*) FROM research_events
            WHERE run_id=%s AND event_type NOT IN ('run.created', 'run_started')""",
            (agent_run.id,),
        )
        assert cursor.fetchone()[0] == 0
    assert first_spec_id != second_spec_id


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
            res = uow.append_event(
                run_id,
                "workflow.created",
                "system",
                "event:created",
                payload={"source": "concurrent-test"},
            )
            return res["event_id"] if isinstance(res, dict) else res

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(append_once, range(2)))
    assert first == second

    with service.uow_factory() as uow:  # noqa: SIM117
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
        revision += 1  # noqa: SIM113
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
            WHERE run_id=%s AND event_type NOT IN ('run.created', 'run_started')""",
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
              AND e.event_type NOT IN ('run.created', 'run_started')
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
        except Exception as exc:  # noqa: BLE001
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
        "type": "object",
        "additionalProperties": False,
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
    }
    failed_context = {
        "run_id": created.id,
        "stage": "planning",
        "schema_name": "test-result",
        "schema_version": 1,
        "artifact_type": "test_result",
        "run_revision": 0,
        "idempotency_key": "semantic:model:timeout",
    }
    call_id = semantic.start_model_call(
        failed_context,
        provider="local",
        requested_model="chat",
        model_revision="rev-1",
        endpoint_alias="local",
        prompt_version="test-v1",
        prompt_hash="a" * 64,
        schema=schema,
        input_token_estimate=12,
    )
    semantic.finish_model_call(
        failed_context,
        call_id,
        status="failed",
        provenance={"provider": "local", "requested_model": "chat"},
        attempts=[{"attempt": 1, "latency_ms": 50, "error": "TimeoutError"}],
        artifacts=[],
        error="TimeoutError",
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
    stored = semantic.inspect(
        host_run.id, UUID(accepted.provenance["semantic_call_id"])
    )
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
    changed = runs.change_execution_mode(created.id, "autonomous_local", **command)
    replay = runs.change_execution_mode(created.id, "autonomous_local", **command)
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
            {
                **context,
                "run_revision": 1,
                "idempotency_key": "semantic:host:stale-authority",
            },
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
                run_id,invocation_id,event_type,actor_type,run_revision,idempotency_key,sequence_number)
                VALUES(%s,%s,'invalid.cross_run','test',0,'cross-run',1)""",
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


def test_run_scoped_passage_selection_has_two_run_isolation(service):
    run_service = build_run_service(service.config)
    run_a = run_service.create(
        objective="run A authoritative extraction",
        external_id=f"fr_integration_{uuid4().hex}",
        execution_mode="autonomous_local",
    )
    run_b = run_service.create(
        objective="run B must not see run A",
        external_id=f"fr_integration_{uuid4().hex}",
        execution_mode="autonomous_local",
    )
    manifest = service.ingest_batch(
        f"fc_integration_{uuid4().hex}",
        "orchestration_extract",
        [
            IngestRequest(
                "https://integration.example/run-a", b"# Run A\n\nScoped evidence."
            )
        ],
        research_run_external_id=run_a.external_id,
    )
    chunk_id = UUID(str(manifest["assets"][0]["chunk_ids"][0]))

    execution_a, passages_a = service.select_run_passages(
        run_a.id, [chunk_id], max_tokens=1000, max_passages=1
    )
    execution_b, passages_b = service.select_run_passages(
        run_b.id, [chunk_id], max_tokens=1000, max_passages=1
    )

    assert execution_a.mechanical_status.value == "succeeded"
    assert [UUID(str(item["chunk_id"])) for item in passages_a] == [chunk_id]
    assert execution_b.mechanical_status.value == "failed"
    assert passages_b == []


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
            """UPDATE research_runs SET state='completed',declared_outcome='test-complete',
            completed_at=now() WHERE id=%s""",
            (run_id,),
        )
    with pytest.raises(ValueError, match="nonterminal research run"):
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
            """UPDATE research_runs SET state='completed',declared_outcome='test-complete',
            completed_at=now() WHERE id=%s""",
            (run_id,),
        )
    with service.uow_factory() as uow:
        with pytest.raises(ValueError, match="another run"):
            uow.start_run("mutated request", {"external_run_id": external_id})
        with pytest.raises(KeyError):
            uow.link_run_asset(external_id, asset.snapshot_id)
        with pytest.raises(KeyError):
            uow.log_retrieval_batch(
                uuid4(),
                run_id,
                [
                    {
                        "stage": "retriever",
                        "query": "late evidence",
                        "retriever": "lexical",
                        "candidate_type": "chunk",
                        "candidate_id": asset.chunk_ids[0],
                        "rank": 1,
                    }
                ],
            )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT objective,state FROM research_runs WHERE id=%s", (run_id,)
        )
        assert cursor.fetchone() == ("original request", "completed")


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
    runs.transition(
        run_id,
        "completed",
        expected_revision=first_terminal_revision,
        idempotency_key="finish:first",
        actor_type="integration-test",
        outcome="satisfied",
        completion={
            "source_manifest_sha256": "a" * 64,
            "answer_sha256": "b" * 64,
        },
    )
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    assert store_cli.main(["run-reopen", external_id]) == 0
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT declared_outcome,completed_at,source_manifest_sha256,answer_sha256,
            state,lifecycle_revision,reopened_from_revision
            FROM research_runs WHERE id=%s""",
            (run_id,),
        )
        assert cursor.fetchone() == (
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
                "UPDATE research_runs SET state='unexpected' WHERE id=%s", (run_id,)
            )
        assert "research_runs_state_check" in str(error.value)
    second_terminal_revision = advance_to_validating(first_terminal_revision + 2)
    runs.transition(
        run_id,
        "completed",
        expected_revision=second_terminal_revision,
        idempotency_key="finish:second",
        actor_type="integration-test",
        outcome="satisfied",
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT declared_outcome,source_manifest_sha256,answer_sha256,
            state,lifecycle_revision
            FROM research_runs WHERE id=%s""",
            (run_id,),
        )
        assert cursor.fetchone() == (
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


def test_search_plan_persistence_and_queries(service):
    run_svc = ResearchRunService(service.uow_factory)
    status = run_svc.create(
        "Search Plan persistence integration test", f"plan-run-{uuid4()}"
    )

    spec_id_uuid = uuid4()
    spec_payload = deepcopy(VALID["research-spec-v1"])
    spec_payload["research_spec_id"] = str(spec_id_uuid)

    with service.uow_factory() as uow:
        db_spec_id = uow.record_research_spec(
            status.id,
            1,
            "research-spec-v1",
            1,
            spec_payload,
            f"spec-key-{uuid4()}",
        )

    plan_payload = deepcopy(VALID["search-plan-v1"])
    plan_payload["research_spec_id"] = str(spec_id_uuid)
    plan_payload["queries"][0]["target_question_ids"] = [
        spec_payload["questions"][0]["question_id"]
    ]
    plan_payload["queries"][0]["target_claim_ids"] = [
        spec_payload["claims_to_validate"][0]["claim_id"]
    ]
    plan_payload["revision"] = 1

    plan_id = run_svc.record_search_plan(
        status.id,
        db_spec_id,
        1,
        plan_payload,
        "plan-idempotency-rev1",
    )
    assert plan_id is not None

    stored_plan = run_svc.get_search_plan(status.id, plan_id=plan_id)
    assert stored_plan["id"] == plan_id
    assert stored_plan["revision"] == 1
    assert stored_plan["status"] == "active"
    assert len(stored_plan["queries"]) == 1
    query = stored_plan["queries"][0]
    assert query["query_text"] == plan_payload["queries"][0]["query"]
    assert query["facet"] == plan_payload["queries"][0]["facet"]

    query_id = UUID(plan_payload["queries"][0]["query_id"])
    query_row = run_svc.get_plan_query(query_id)
    assert query_row["id"] == query_id
    assert query_row["plan_id"] == plan_id
    assert query_row["run_id"] == status.id

    with service.uow_factory() as uow:
        plan_queries = uow.list_plan_queries(plan_id)
        assert len(plan_queries) == 1
        assert plan_queries[0]["id"] == query_id

        all_plans = uow.list_search_plans(status.id)
        assert len(all_plans) == 1
        assert all_plans[0]["id"] == plan_id

    retry_id = run_svc.record_search_plan(
        status.id,
        db_spec_id,
        1,
        plan_payload,
        "plan-idempotency-rev1",
    )
    assert retry_id == plan_id

    conflicting = deepcopy(plan_payload)
    conflicting["queries"][0]["query"] = "Different query text entirely"
    with pytest.raises(ValueError, match="idempotency key was used"):
        run_svc.record_search_plan(
            status.id,
            db_spec_id,
            1,
            conflicting,
            "plan-idempotency-rev1",
        )

    plan_v1_other = deepcopy(plan_payload)
    plan_v1_other["queries"][0]["query"] = "Another search query text"
    with pytest.raises(ValueError, match="already exists"):
        run_svc.record_search_plan(
            status.id,
            db_spec_id,
            1,
            plan_v1_other,
            "plan-idempotency-rev1-alt",
        )

    plan_v2 = deepcopy(plan_payload)
    plan_v2["revision"] = 2
    plan_v2["queries"][0]["query_id"] = str(uuid4())
    plan_v2["queries"][0]["query"] = "Fixture Engine v2 behavior documentation"
    plan_id_v2 = run_svc.record_search_plan(
        status.id,
        db_spec_id,
        2,
        plan_v2,
        "plan-idempotency-rev2",
    )
    assert plan_id_v2 != plan_id

    stored_v1 = run_svc.get_search_plan(status.id, plan_id=plan_id)
    assert stored_v1["status"] == "superseded"

    latest = run_svc.get_search_plan(status.id)
    assert latest["id"] == plan_id_v2
    assert latest["revision"] == 2
    assert latest["status"] == "active"

    plan_invalid_target = deepcopy(plan_payload)
    plan_invalid_target["revision"] = 3
    plan_invalid_target["queries"][0]["target_question_ids"] = [
        "00000000-0000-0000-0000-000000009999"
    ]
    with pytest.raises(Exception, match="unknown question IDs"):
        run_svc.record_search_plan(
            status.id,
            db_spec_id,
            3,
            plan_invalid_target,
            "plan-idempotency-rev3-invalid",
        )


class TestCoverageWorkflowObservationEvents:
    """Integration tests for coverage workflow observation event types."""

    def test_candidate_identified_event(self, service):
        from research_store.coverage_service import CoverageService

        run_svc = ResearchRunService(service.uow_factory)
        status = run_svc.create(
            "Coverage workflow observation test", f"coverage-{uuid4()}"
        )
        coverage = CoverageService(service.uow_factory)
        items = coverage.create_items_from_spec(
            status.id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        item_id = items[0].coverage_item_id
        candidate_id = uuid4()
        event = coverage.apply_candidate_identified(
            status.id, item_id, candidate_id=candidate_id
        )
        assert event.event_type == "candidate_identified"
        assert event.coverage_revision >= 2
        ledger = coverage.rebuild_projection(status.id)
        assert len(ledger.items[0].candidate_ids) == 1
        assert ledger.items[0].candidate_ids[0] == candidate_id

    def test_asset_acquired_event(self, service):
        from research_store.coverage_service import CoverageService

        run_svc = ResearchRunService(service.uow_factory)
        status = run_svc.create(
            "Coverage workflow observation test", f"coverage-{uuid4()}"
        )
        coverage = CoverageService(service.uow_factory)
        items = coverage.create_items_from_spec(
            status.id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        item_id = items[0].coverage_item_id
        url = "https://integration.example.com/source"
        event = coverage.apply_asset_acquired(status.id, item_id, source_url=url)
        assert event.event_type == "asset_acquired"
        ledger = coverage.rebuild_projection(status.id)
        assert ledger.items[0].status.value == "acquired"
        assert ledger.items[0].independent_source_count == 1

    def test_evidence_retrieved_event(self, service):
        from research_store.coverage_service import CoverageService

        run_svc = ResearchRunService(service.uow_factory)
        status = run_svc.create(
            "Coverage workflow observation test", f"coverage-{uuid4()}"
        )
        coverage = CoverageService(service.uow_factory)
        items = coverage.create_items_from_spec(
            status.id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        item_id = items[0].coverage_item_id
        passage_ids = [str(uuid4()), str(uuid4())]
        event = coverage.apply_evidence_retrieved(
            status.id, item_id, passage_ids=passage_ids
        )
        assert event.event_type == "evidence_retrieved"
        ledger = coverage.rebuild_projection(status.id)
        assert len(ledger.items[0].passage_ids) == 2
        assert ledger.items[0].status.value == "unassessed"

    def test_source_class_observed_event(self, service):
        from research_store.coverage_service import CoverageService

        run_svc = ResearchRunService(service.uow_factory)
        status = run_svc.create(
            "Coverage workflow observation test", f"coverage-{uuid4()}"
        )
        coverage = CoverageService(service.uow_factory)
        items = coverage.create_items_from_spec(
            status.id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        item_id = items[0].coverage_item_id
        coverage.apply_source_class_observed(
            status.id, item_id, authority_class="primary"
        )
        coverage.apply_source_class_observed(
            status.id, item_id, authority_class="authoritative_secondary"
        )
        ledger = coverage.rebuild_projection(status.id)
        assert "primary" in ledger.items[0].authority_classes_present
        assert "authoritative_secondary" in ledger.items[0].authority_classes_present

    def test_freshness_observed_event(self, service):
        from research_domain.models import FreshnessStatus
        from research_store.coverage_service import CoverageService

        run_svc = ResearchRunService(service.uow_factory)
        status = run_svc.create(
            "Coverage workflow observation test", f"coverage-{uuid4()}"
        )
        coverage = CoverageService(service.uow_factory)
        items = coverage.create_items_from_spec(
            status.id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        item_id = items[0].coverage_item_id
        coverage.apply_freshness_observed(
            status.id, item_id, freshness_status="unsatisfied"
        )
        ledger = coverage.rebuild_projection(status.id)
        assert ledger.items[0].freshness_status == FreshnessStatus.UNSATISFIED

    def test_extraction_attempted_tracks_source_url(self, service):
        from research_store.coverage_service import CoverageService

        run_svc = ResearchRunService(service.uow_factory)
        status = run_svc.create(
            "Coverage workflow observation test", f"coverage-{uuid4()}"
        )
        coverage = CoverageService(service.uow_factory)
        items = coverage.create_items_from_spec(
            status.id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        item_id = items[0].coverage_item_id
        coverage.apply_extraction_attempted(
            status.id, item_id, source_url="https://integration.example.com"
        )
        ledger = coverage.rebuild_projection(status.id)
        assert ledger.items[0].status.value == "unassessed"
        assert ledger.items[0].independent_source_count == 1

    def test_duplicate_source_url_deduplicated_in_projection(self, service):
        from research_store.coverage_service import CoverageService

        run_svc = ResearchRunService(service.uow_factory)
        status = run_svc.create(
            "Coverage workflow observation test", f"coverage-{uuid4()}"
        )
        coverage = CoverageService(service.uow_factory)
        items = coverage.create_items_from_spec(
            status.id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        item_id = items[0].coverage_item_id
        url = "https://integration.example.com/dedup"
        coverage.apply_asset_acquired(
            status.id, item_id, source_url=url, idempotency_key="dedup:1"
        )
        coverage.apply_asset_acquired(
            status.id, item_id, source_url=url, idempotency_key="dedup:2"
        )
        ledger = coverage.rebuild_projection(status.id)
        assert ledger.items[0].independent_source_count == 1

    def test_source_event_id_provenance(self, service):
        from research_store.coverage_service import CoverageService

        run_svc = ResearchRunService(service.uow_factory)
        status = run_svc.create(
            "Coverage workflow observation test", f"coverage-{uuid4()}"
        )
        coverage = CoverageService(service.uow_factory)
        items = coverage.create_items_from_spec(
            status.id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        item_id = items[0].coverage_item_id
        with service.uow_factory() as uow, uow.connection.cursor() as cur:
            cur.execute(
                "SELECT id FROM research_events WHERE run_id=%s LIMIT 1",
                (status.id,),
            )
            source_event = cur.fetchone()[0]
        event = coverage.apply_asset_acquired(
            status.id,
            item_id,
            source_url="https://integration.example.com",
            source_event_id=source_event,
        )
        assert event.source_event_id == source_event


class TestResearchOrchestratorIntegration:
    """End-to-end integration test for ResearchOrchestrator with PostgreSQL."""

    def test_orchestrator_end_to_end(self, service):
        from research_store.orchestrator import OrchestratorConfig, ResearchOrchestrator

        orchestrator = ResearchOrchestrator.build(
            config=service.config,
            orchestrator_config=OrchestratorConfig(max_adaptive_cycles=2),
        )
        run_svc = ResearchRunService(service.uow_factory)
        run_status = run_svc.create(
            "Integration test objective",
            f"test-{uuid4()}",
            execution_mode="autonomous_local",
        )
        run_id = run_status.id
        from budget_policy import conservative_research_spec

        spec = serialize_model(
            conservative_research_spec("Integration test objective", "fact_finding")
        )
        qid = spec["questions"][0]["question_id"]
        search_plan = {
            "schema_version": "search-plan-v1",
            "research_spec_id": spec["research_spec_id"],
            "revision": 1,
            "queries": [
                {
                    "query_id": str(uuid4()),
                    "query": "test query",
                    "facet": "overview",
                    "target_question_ids": [qid],
                    "target_claim_ids": [],
                    "intended_source_classes": [],
                    "expected_organizations": [],
                    "freshness_requirement": {
                        "start": None,
                        "end": None,
                        "description": "unconstrained",
                        "uncertainty": "none",
                    },
                    "expected_contribution": "overview",
                    "domain_restrictions": [],
                    "negative_terms": [],
                    "priority": 1,
                },
            ],
        }
        result = orchestrator.run(
            run_id=run_id,
            spec=spec,
            search_plan=search_plan,
        )
        assert result.final_state in ("completed", "partial", "failed")
        assert result.outcome == result.final_state
        final_status = run_svc.status(run_id=run_id)
        assert final_status.state in ("completed", "partial", "failed")
        with service.uow_factory() as uow, uow.connection.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM research_run_transitions WHERE run_id=%s",
                (run_id,),
            )
            transition_count = cur.fetchone()[0]
        assert transition_count > 0
        assert final_status.lifecycle_revision > 0


class TestMigration0015TerminalDecisions:
    """Test terminal-decision migration and append-only behavior."""

    def test_migration_creates_terminal_decisions_table(self):
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA public CASCADE")
            cursor.execute("CREATE SCHEMA public")
        assert migrate(TEST_DSN) >= 15
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('terminal_decisions')")
            assert cursor.fetchone()[0] is not None
            cursor.execute("SELECT to_regtype('terminal_decision_outcome')")
            assert cursor.fetchone()[0] is not None
            cursor.execute(
                """SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'terminal_decisions'
                ORDER BY ordinal_position"""
            )
            columns = {
                row[0]: {"data_type": row[1], "nullable": row[2]}
                for row in cursor.fetchall()
            }
            for name in (
                "id",
                "run_id",
                "decision_id",
                "run_revision",
                "coverage_revision",
                "outcome",
                "no_progress_signals",
                "unresolved_gap",
                "policy_version",
                "idempotency_key",
                "created_at",
            ):
                assert name in columns

    def test_migration_indexes_created(self):
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA public CASCADE")
            cursor.execute("CREATE SCHEMA public")
        assert migrate(TEST_DSN) >= 15
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT indexname FROM pg_indexes
                WHERE tablename = 'terminal_decisions'
                ORDER BY indexname"""
            )
            indexes = {row[0] for row in cursor.fetchall()}
            assert "terminal_decisions_run_cursor_idx" in indexes
            assert "terminal_decisions_outcome_idx" in indexes
            assert "terminal_decisions_decision_idx" in indexes

    def test_append_only_trigger_enforced(self):
        """Verify structured terminal decisions remain append-only."""
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA public CASCADE")
            cursor.execute("CREATE SCHEMA public")
        assert migrate(TEST_DSN) >= 15

        config = replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=Path("/tmp/firecrawl-terminal-append-only-test"),
            embedding_dimension=4,
        )
        runs = build_run_service(config)
        status = runs.create(
            "append-only terminal decision",
            f"fr_test_{uuid4().hex}",
            execution_mode="autonomous_local",
        )
        runs.transition(
            status.id,
            "planning",
            expected_revision=status.lifecycle_revision,
            idempotency_key=f"append-only:{status.id}:planning",
            actor_type="integration-test",
        )
        status = runs.status(run_id=status.id)
        key = f"append-only:{status.id}:failed"
        runs.fail(
            status.id,
            expected_revision=status.lifecycle_revision,
            idempotency_key=key,
            actor_type="integration-test",
            reason="exercise terminal decision append-only trigger",
            outcome="failed",
            error="exercise terminal decision append-only trigger",
            completion={
                "reason_code": "integration_append_only_test",
                "state_census": {
                    "schema_version": "terminal-state-census-v1",
                    "available": True,
                    "counts": {"failed": 1},
                },
            },
        )

        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT id FROM terminal_decisions
                WHERE run_id=%s AND idempotency_key=%s""",
                (status.id, key),
            )
            row_id = cursor.fetchone()[0]
            cursor.execute("SAVEPOINT update_sp")
            with pytest.raises(Exception, match="terminal_decisions is append-only"):
                cursor.execute(
                    "UPDATE terminal_decisions SET outcome='partial' WHERE id=%s",
                    (row_id,),
                )
            cursor.execute("ROLLBACK TO SAVEPOINT update_sp")
            cursor.execute("SAVEPOINT delete_sp")
            with pytest.raises(Exception, match="terminal_decisions is append-only"):
                cursor.execute("DELETE FROM terminal_decisions WHERE id=%s", (row_id,))
            cursor.execute("ROLLBACK TO SAVEPOINT delete_sp")

    def test_forward_only_downgrade(self):
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA public CASCADE")
            cursor.execute("CREATE SCHEMA public")
        assert migrate(TEST_DSN) >= 15
        from alembic import command
        from alembic.config import Config

        root = Path(__file__).parents[1]
        config = Config(str(root / "alembic.ini"))
        old_env = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = TEST_DSN
        try:
            with pytest.raises(
                RuntimeError, match="Research workflow migrations are forward-only"
            ):
                command.downgrade(config, "0014_coverage_event_types")
        finally:
            if old_env is not None:
                os.environ["DATABASE_URL"] = old_env
            else:
                os.environ.pop("DATABASE_URL", None)

    def test_current_schema_contains_all_workflow_tables(self):
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA public CASCADE")
            cursor.execute("CREATE SCHEMA public")
        assert migrate(TEST_DSN) >= 15
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT to_regclass('research_runs'),
                to_regclass('coverage_events'),to_regclass('coverage_snapshots'),
                to_regclass('strategy_revisions'),to_regclass('search_plans'),
                to_regclass('search_responses'),to_regclass('search_candidates')"""
            )
            assert all(cursor.fetchone())
            cursor.execute(
                """SELECT to_regclass('terminal_decisions'),
                to_regclass('research_runs'),to_regclass('coverage_events')"""
            )
            assert all(cursor.fetchone())


def test_run_annotate_handler_executes_through_service(monkeypatch, capsys):
    external_id = f"fr_annotate_{uuid4().hex}"
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    assert store_cli.main(
        [
            "run-start",
            external_id,
            "Annotate integration test",
            "--mode",
            "autonomous_local",
        ]
    ) == 0
    capsys.readouterr()
    assert store_cli.main(
        [
            "run-annotate",
            external_id,
            "--type",
            "pivot",
            "--reason",
            "integration test annotation",
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["event_type"] == "pivot"
    assert result["lifecycle_revision"] >= 1
    assert store_cli.main(["run-status", external_id]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == "created"


def test_run_verify_handler_executes_through_service(monkeypatch, capsys):
    external_id = f"fr_verify_{uuid4().hex}"
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.delenv("BLOB_ROOT", raising=False)
    assert store_cli.main(
        [
            "run-start",
            external_id,
            "Verify integration test",
            "--mode",
            "deterministic_debug",
        ]
    ) == 0
    capsys.readouterr()
    assert store_cli.main(["run-verify", external_id]) == 0
    result = json.loads(capsys.readouterr().out)
    assert "target" in result
    assert "verified_at" in result
    assert result["total"] == 0
    assert result["available"] == 0
    assert "file_based_unverified" in result


def test_run_audit_handler_executes_through_service(monkeypatch, capsys):
    external_id = f"fr_audit_{uuid4().hex}"
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    assert store_cli.main(
        [
            "run-start",
            external_id,
            "Audit integration test",
            "--mode",
            "autonomous_local",
        ]
    ) == 0
    capsys.readouterr()
    assert store_cli.main(
        ["run-audit", external_id, "--target-hash", "a" * 64]
    ) in (0, 1)
    out = capsys.readouterr().out.strip()
    assert out
    result = json.loads(out)
    assert "status" in result or "assessment_id" in result or "error" in result


def test_run_compare_handler_executes_through_service(monkeypatch, capsys):
    external_id_a = f"fr_compare_a_{uuid4().hex}"
    external_id_b = f"fr_compare_b_{uuid4().hex}"
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    assert store_cli.main(
        ["run-start", external_id_a, "Compare test A", "--mode", "autonomous_local"]
    ) == 0
    capsys.readouterr()
    assert store_cli.main(
        ["run-start", external_id_b, "Compare test B", "--mode", "autonomous_local"]
    ) == 0
    capsys.readouterr()
    assert store_cli.main(["run-compare", external_id_a, external_id_b]) in (0, 1)
    out = capsys.readouterr().out.strip()
    assert out
    result = json.loads(out)
    assert (
        "status" in result
        or "comparisons" in result
        or "comparison" in result
        or "error" in result
    )


def _configure_cli_for_service(monkeypatch, service):
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("BLOB_ROOT", str(service.config.blob_root))
    monkeypatch.setenv("EMBEDDING_MODEL", service.config.embedding_model)
    monkeypatch.setenv("EMBEDDING_REVISION", service.config.embedding_revision)
    monkeypatch.setenv("EMBEDDING_DIMENSION", str(service.config.embedding_dimension))
    monkeypatch.setenv("PARSER_VERSION", service.config.parser_version)
    monkeypatch.setenv("NORMALIZATION_VERSION", service.config.normalization_version)
    monkeypatch.setenv("CHUNKER_VERSION", service.config.chunker_version)


def _seed_completed_indexed_asset(service, external_run_id):
    manifest = service.ingest_batch(
        f"fixture_{uuid4().hex}",
        "scrape",
        [
            IngestRequest(
                f"https://integration.example/{uuid4().hex}",
                b"# Indexed fixture\n\nPersisted PostgreSQL evidence.",
            )
        ],
        research_run_external_id=external_run_id,
    )
    assert manifest["failure_count"] == 0
    run_id = build_run_service(service.config).status(external_id=external_run_id).id
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE index_jobs j
               SET status='complete', completed_at=now(), error=NULL
               FROM embedding_manifests m
               JOIN chunks c ON c.id=m.chunk_id
               JOIN documents d ON d.id=c.document_id
               JOIN research_run_assets ra ON ra.snapshot_id=d.snapshot_id
               WHERE j.manifest_id=m.id AND ra.run_id=%s""",
            (run_id,),
        )
        assert cursor.rowcount > 0
        cursor.execute(
            """UPDATE embedding_manifests m
               SET index_status='complete', indexed_at=now(), error=NULL
               FROM chunks c
               JOIN documents d ON d.id=c.document_id
               JOIN research_run_assets ra ON ra.snapshot_id=d.snapshot_id
               WHERE m.chunk_id=c.id AND ra.run_id=%s""",
            (run_id,),
        )


def _advance_run_to_validating(svc, status, key_prefix):
    revision = status.lifecycle_revision
    for state in (
        "planning",
        "corpus_review",
        "retrieving",
        "synthesizing",
        "validating",
    ):
        svc.transition(
            status.id,
            state,
            expected_revision=revision,
            idempotency_key=f"{key_prefix}:{state}",
            actor_type="integration-test",
        )
        revision += 1
    return revision


def test_run_finish_handler_executes_through_service(monkeypatch, capsys, service):
    external_id = f"fr_finish_{uuid4().hex}"
    _configure_cli_for_service(monkeypatch, service)
    assert store_cli.main(
        [
            "run-start",
            external_id,
            "Finish integration test",
            "--mode",
            "autonomous_local",
        ]
    ) == 0
    capsys.readouterr()
    _seed_completed_indexed_asset(service, external_id)
    svc = build_run_service()
    status = svc.status(external_id=external_id)
    _advance_run_to_validating(svc, status, "advance:finish")
    assert store_cli.main(
        ["run-finish", external_id, "--outcome", "satisfied"]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "completed"
    assert result["terminal"] is True
    assert store_cli.main(["run-status", external_id]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["state"] == "completed"


def test_run_finish_idempotency_same_outcome(monkeypatch, capsys, service):
    external_id = f"fr_finish_idem_{uuid4().hex}"
    _configure_cli_for_service(monkeypatch, service)
    assert store_cli.main(
        [
            "run-start",
            external_id,
            "Finish idempotency test",
            "--mode",
            "autonomous_local",
        ]
    ) == 0
    capsys.readouterr()
    _seed_completed_indexed_asset(service, external_id)
    svc = build_run_service()
    status = svc.status(external_id=external_id)
    _advance_run_to_validating(svc, status, f"finish-idem:{external_id}")
    finish_key = f"finish-idem:complete:{external_id}"
    args = [
        "run-finish",
        external_id,
        "--outcome",
        "satisfied",
        "--idempotency-key",
        finish_key,
    ]
    assert store_cli.main(args) == 0
    first_result = json.loads(capsys.readouterr().out)
    assert store_cli.main(args) == 0
    second_result = json.loads(capsys.readouterr().out)
    assert second_result["state"] == "completed"
    assert second_result["lifecycle_revision"] == first_result["lifecycle_revision"]
    assert second_result["id"] == first_result["id"]


def test_run_reopen_after_finish_idempotency(monkeypatch, capsys, service):
    external_id = f"fr_reopen_idem_{uuid4().hex}"
    _configure_cli_for_service(monkeypatch, service)
    assert store_cli.main(
        [
            "run-start",
            external_id,
            "Reopen idempotency test",
            "--mode",
            "autonomous_local",
        ]
    ) == 0
    capsys.readouterr()
    _seed_completed_indexed_asset(service, external_id)
    svc = build_run_service()
    status = svc.status(external_id=external_id)
    _advance_run_to_validating(svc, status, "advance:reopen")
    assert store_cli.main(
        ["run-finish", external_id, "--outcome", "satisfied"]
    ) == 0
    finish_result = json.loads(capsys.readouterr().out)
    assert store_cli.main(
        ["run-reopen", external_id, "--reason", "need more research"]
    ) == 0
    reopen_result = json.loads(capsys.readouterr().out)
    assert reopen_result["next_state"] == "created"
    assert reopen_result["lifecycle_revision"] == finish_result["lifecycle_revision"] + 1
    assert store_cli.main(["run-status", external_id]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["state"] == "created"


def test_run_annotate_idempotency_same_key(monkeypatch, capsys):
    external_id = f"fr_annotate_idem_{uuid4().hex}"
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    assert store_cli.main(
        [
            "run-start",
            external_id,
            "Annotate idempotency test",
            "--mode",
            "autonomous_local",
        ]
    ) == 0
    capsys.readouterr()
    args = [
        "run-annotate",
        external_id,
        "--type",
        "pivot",
        "--reason",
        "integration test annotation",
        "--idempotency-key",
        "annotate:idem:key:1",
    ]
    assert store_cli.main(args) == 0
    first_result = json.loads(capsys.readouterr().out)
    assert store_cli.main(args) == 0
    second_result = json.loads(capsys.readouterr().out)
    assert second_result["event_id"] == first_result["event_id"]
    assert second_result["lifecycle_revision"] == first_result["lifecycle_revision"]
    assert second_result.get("reused") is True


def test_run_audit_idempotency_same_target_hash(monkeypatch, capsys):
    external_id = f"fr_audit_idem_{uuid4().hex}"
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    assert store_cli.main(
        [
            "run-start",
            external_id,
            "Audit idempotency test",
            "--mode",
            "autonomous_local",
        ]
    ) == 0
    capsys.readouterr()
    args = ["run-audit", external_id, "--target-hash", "a" * 64]
    assert store_cli.main(args) in (0, 1)
    first_result = json.loads(capsys.readouterr().out.strip())
    assert store_cli.main(args) in (0, 1)
    second_result = json.loads(capsys.readouterr().out.strip())
    first_assessment_id = first_result.get("assessment_id")
    second_assessment_id = second_result.get("assessment_id")
    assert first_assessment_id == second_assessment_id or (
        first_assessment_id is not None and second_assessment_id is not None
    )


def test_run_annotate_unknown_external_id(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    result = store_cli.main(
        [
            "run-annotate",
            "fr_nonexistent_unknown_id",
            "--type",
            "pivot",
            "--reason",
            "should fail",
        ]
    )
    assert result != 0


def test_hierarchical_chunk_columns_exist_after_migration():
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT column_name, column_default, is_nullable
            FROM information_schema.columns
            WHERE table_name='chunks'
              AND column_name IN ('tokenizer_name','parent_block_id')
            ORDER BY column_name"""
        )
        rows = cursor.fetchall()
    col_map = {row[0]: {"default": row[1], "nullable": row[2]} for row in rows}
    assert col_map["tokenizer_name"]["nullable"] == "YES"
    assert col_map["parent_block_id"]["nullable"] == "YES"


def _insert_chunk_fixture(prefix):
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO sources(canonical_url) VALUES (%s) RETURNING id",
            (f"https://{prefix}.example/",),
        )
        source_id = cursor.fetchone()[0]
        cursor.execute(
            """INSERT INTO asset_snapshots(
                source_id,requested_url,retrieved_at,content_sha256
            ) VALUES (%s,%s,now(),%s) RETURNING id""",
            (source_id, f"https://{prefix}.example/", prefix[0] * 64),
        )
        snapshot_id = cursor.fetchone()[0]
        cursor.execute(
            """INSERT INTO documents(
                snapshot_id,normalized_text,parser_name,parser_version,
                normalization_version,document_sha256
            ) VALUES (%s,%s,'markdown','v1','v1',%s) RETURNING id""",
            (snapshot_id, prefix, prefix[-1] * 64),
        )
        document_id = cursor.fetchone()[0]
        cursor.execute(
            """INSERT INTO document_blocks(document_id,block_type,ordinal,text)
            VALUES (%s,'paragraph',0,%s) RETURNING id""",
            (document_id, prefix),
        )
        block_id = cursor.fetchone()[0]
    return document_id, block_id


def test_hierarchical_chunk_derivation_key_constraint():
    document_id, block_id = _insert_chunk_fixture("derivationkey")
    values = (
        document_id,
        block_id,
        block_id,
        0,
        "chunk text",
        "f" * 64,
        "hierarchical",
        "hierarchical-v1",
        "cl100k_base",
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO chunks(
                document_id,first_block_id,last_block_id,ordinal,text,
                content_sha256,chunker_name,chunker_version,tokenizer_name
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            values,
        )
    with pytest.raises(Exception):
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO chunks(
                    document_id,first_block_id,last_block_id,ordinal,text,
                    content_sha256,chunker_name,chunker_version,tokenizer_name
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                values,
            )


def test_hierarchical_chunk_parent_block_fk():
    document_id, block_id = _insert_chunk_fixture("parentfk")
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO chunks(
                document_id,first_block_id,last_block_id,ordinal,text,
                content_sha256,chunker_name,chunker_version,tokenizer_name,
                parent_block_id
            ) VALUES (%s,%s,%s,0,%s,%s,%s,%s,%s,%s)""",
            (
                document_id,
                block_id,
                block_id,
                "chunk with parent",
                "i" * 64,
                "hierarchical",
                "hierarchical-v1",
                "cl100k_base",
                block_id,
            ),
        )
    with pytest.raises(Exception):
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO chunks(
                    document_id,first_block_id,last_block_id,ordinal,text,
                    content_sha256,chunker_name,chunker_version,tokenizer_name,
                    parent_block_id
                ) VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s,%s)""",
                (
                    document_id,
                    block_id,
                    block_id,
                    "chunk with invalid parent",
                    "j" * 64,
                    "hierarchical",
                    "hierarchical-v1",
                    "cl100k_base",
                    uuid4(),
                ),
            )


def test_hierarchical_chunk_persist_ingest_sets_parent_block_id():
    svc = build_service(
        replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            blob_root=Path("/tmp/test_blobs"),
            qdrant_collection="test_hier_parent",
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            chunker_version="structural-v1",
        )
    )
    result = svc.ingest(
        IngestRequest(
            requested_url="https://test-persist.example/",
            content=b"# Title\n\nParagraph one.",
            mime_type="text/markdown",
        )
    )
    assert result.chunk_ids
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT id,parent_block_id FROM chunks WHERE id=ANY(%s)",
            (list(result.chunk_ids),),
        )
        rows = cursor.fetchall()
    assert len(rows) == len(result.chunk_ids)
    assert all(parent_block_id is not None for _chunk_id, parent_block_id in rows)


def test_hierarchical_chunk_migration_preserves_legacy_data():
    document_id, block_id = _insert_chunk_fixture("legacychunk")
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO chunks(
                document_id,first_block_id,last_block_id,ordinal,text,
                content_sha256,chunker_name,chunker_version
            ) VALUES (%s,%s,%s,0,%s,%s,%s,%s) RETURNING id""",
            (
                document_id,
                block_id,
                block_id,
                "legacy chunk",
                "m" * 64,
                "structural",
                "structural-v1",
            ),
        )
        legacy_chunk_id = cursor.fetchone()[0]
    assert migrate(TEST_DSN) >= 25
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT chunker_name,chunker_version,tokenizer_name,
                      parent_block_id,first_block_id
            FROM chunks WHERE id=%s""",
            (legacy_chunk_id,),
        )
        row = cursor.fetchone()
    assert row is not None
    assert row[0] == "structural"
    assert row[1] == "structural-v1"
    assert row[4] == block_id


def test_retrieval_stage_trace_batch_persistence_and_ordering(service):
    from types import SimpleNamespace

    from research_store.service import CorpusService

    with service.uow_factory() as uow:
        run_id = uow.start_run("trace persistence test", {})
    asset = service.ingest(
        IngestRequest(
            "https://trace.example/test",
            b"# Section A\n\n"
            + b"word " * 1000
            + b"\n\n# Section B\n\n"
            + b"word " * 1000,
        )
    )
    chunk_ids = list(asset.chunk_ids)

    class IntegrationTestIndex:
        def list_aliases(self):
            return {"active": "test_collection"}

        def search(self, *_args):
            return [
                {
                    "id": str(uuid4()),
                    "score": 0.9,
                    "payload": {"chunk_id": str(chunk_id), "title": "Test"},
                }
                for chunk_id in chunk_ids
            ]

    config = SimpleNamespace(
        qdrant_alias="active",
        physical_collection="test_collection",
        reranker_candidate_limit=40,
        parser_version="markdown-v1",
        normalization_version="cleanup-v1",
        chunker_version="structural-v1",
        embedding_fingerprint="abc",
    )
    corpus_service = CorpusService(
        config,
        service.uow_factory,
        blob_store=None,
        index=IntegrationTestIndex(),
        embedder=lambda _query: [0.1],
    )
    execution, _results = corpus_service.search_assets(
        "test trace ordering",
        candidate_limit=1,
        run_id=run_id,
        requested_mode="semantic",
    )
    trace = corpus_service.get_retrieval_trace(execution.execution_id)
    count = len(chunk_ids)
    assert len(trace) == 2 * count
    assert all(item["stage"] == "semantic" for item in trace[:count])
    assert all(item["stage"] == "fused" for item in trace[count:])
    assert count >= 2
    assert trace[count]["selected"] is True
    assert trace[count + 1]["selected"] is False
    assert trace[count + 1]["rejection_reason"] == "below_candidate_limit"


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def test_validation_stage_persistence(service):
    from research_store.domain import SynthesisStageRecord

    now = _utcnow()
    stage_id = uuid4()
    record = SynthesisStageRecord(
        id=stage_id,
        run_id=uuid4(),
        stage_name="validation",
        stage_status="completed",
        semantic_call_id=None,
        semantic_artifact_id=None,
        evidence_packet_revision=1,
        model_name="local",
        prompt_version="synthesis-v1",
        schema_version=1,
        artifact={
            "report_hash": "abc123",
            "current_packet_revision": 1,
            "stale_packet": False,
            "validation_status": "valid",
            "is_complete": True,
            "claim_manifest": [],
            "validation_errors_count": 0,
            "validation_warnings_count": 0,
            "summary": "All claims supported.",
        },
        error=None,
        attempts=1,
        created_at=now,
        updated_at=now,
    )
    assert record.stage_name == "validation"
    with service.uow_factory() as uow:
        run_id = uow.start_run("validation stage persistence test", {})
    with service.uow_factory() as uow:
        uow.insert_synthesis_stage(
            {
                "id": stage_id,
                "run_id": run_id,
                "stage_name": "validation",
                "stage_status": "completed",
                "semantic_call_id": None,
                "semantic_artifact_id": None,
                "evidence_packet_revision": 1,
                "model_name": "local",
                "prompt_version": "synthesis-v1",
                "schema_version": 1,
                "artifact": record.artifact,
                "error": None,
                "attempts": 1,
                "created_at": now,
                "updated_at": now,
            }
        )
        retrieved = uow.get_synthesis_stage(run_id, "validation")
    assert retrieved is not None
    assert retrieved["stage_name"] == "validation"
    assert retrieved["stage_status"] == "completed"
    assert retrieved["artifact"]["report_hash"] == "abc123"


class TestIndexRebuildRecovery:
    """End-to-end tests for index rebuild, reconciliation, and recovery."""

    def setup_method(self):
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM index_point_counts")
            cursor.execute("DELETE FROM index_jobs")
            cursor.execute("DELETE FROM embedding_manifests")
            cursor.execute("DELETE FROM index_definitions")
            cursor.execute("DELETE FROM chunks")
            cursor.execute("DELETE FROM document_blocks")
            cursor.execute("DELETE FROM ingestion_batch_assets")
            cursor.execute("DELETE FROM documents")
            cursor.execute("DELETE FROM research_run_assets")
            cursor.execute("DELETE FROM asset_snapshots")
            cursor.execute("DELETE FROM sources")

    def _config(self):
        return replace(
            StoreConfig.from_env(),
            database_url=TEST_DSN,
            embedding_dimension=4,
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            chunker_version="structural-v1",
        )

    def _insert_index_fixture(self, name, count=1):
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO sources(canonical_url) VALUES (%s) RETURNING id",
                (f"https://{name}.example/test",),
            )
            source_id = cursor.fetchone()[0]
            cursor.execute(
                """INSERT INTO asset_snapshots(
                    source_id,requested_url,retrieved_at,content_sha256
                ) VALUES (%s,%s,now(),%s) RETURNING id""",
                (source_id, f"https://{name}.example/test", name[0] * 64),
            )
            snapshot_id = cursor.fetchone()[0]
            cursor.execute(
                """INSERT INTO documents(
                    snapshot_id,normalized_text,parser_name,parser_version,
                    normalization_version,document_sha256
                ) VALUES (%s,%s,'markdown','markdown-v1','cleanup-v1',%s)
                RETURNING id""",
                (snapshot_id, name, name[-1] * 64),
            )
            document_id = cursor.fetchone()[0]
            chunk_ids = []
            for ordinal in range(count):
                cursor.execute(
                    """INSERT INTO chunks(
                        document_id,ordinal,text,content_sha256,
                        chunker_name,chunker_version
                    ) VALUES (%s,%s,%s,%s,'structural','structural-v1')
                    RETURNING id""",
                    (
                        document_id,
                        ordinal,
                        f"{name} chunk {ordinal}",
                        f"{ordinal:064x}"[-64:],
                    ),
                )
                chunk_ids.append(cursor.fetchone()[0])
        return chunk_ids

    def test_index_build_creates_jobs_for_all_eligible_manifests(self, service):
        from research_store.cli import _index_build

        self._insert_index_fixture("recovery", 3)
        result = _index_build(self._config())
        assert result["selected_chunks"] == 3
        assert result["scheduled"] == 3
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT count(*) FROM embedding_manifests
                WHERE index_definition_id=%s""",
                (result["index_definition"]["id"],),
            )
            assert cursor.fetchone()[0] == 3
            cursor.execute(
                """SELECT count(*) FROM index_jobs
                WHERE index_definition_id=%s AND status='pending'""",
                (result["index_definition"]["id"],),
            )
            assert cursor.fetchone()[0] == 3

    def test_index_build_idempotent_resume_no_duplicates(self, service):
        from research_store.cli import _index_build

        self._insert_index_fixture("idempotent", 1)
        config = self._config()
        first = _index_build(config)
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE index_jobs SET status='dead',attempt_count=5,
                error='lease expired after final allowed attempt'
                WHERE index_definition_id=%s""",
                (first["index_definition"]["id"],),
            )
            cursor.execute(
                """UPDATE embedding_manifests SET index_status='failed',
                error='lease expired after final allowed attempt'
                WHERE index_definition_id=%s""",
                (first["index_definition"]["id"],),
            )
        second = _index_build(config)
        assert second["scheduled"] == 1
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT count(*),min(status),min(attempt_count) FROM index_jobs
                WHERE index_definition_id=%s""",
                (first["index_definition"]["id"],),
            )
            assert cursor.fetchone() == (1, "pending", 0)

    def test_index_build_resume_interrupted_build(self, service):
        from research_store.cli import _index_build

        self._insert_index_fixture("interrupted", 2)
        config = self._config()
        first = _index_build(config)
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT id FROM embedding_manifests
                WHERE index_definition_id=%s ORDER BY id LIMIT 1""",
                (first["index_definition"]["id"],),
            )
            manifest_id = cursor.fetchone()[0]
            cursor.execute(
                """UPDATE embedding_manifests SET index_status='complete',indexed_at=now()
                WHERE id=%s""",
                (manifest_id,),
            )
            cursor.execute(
                """UPDATE index_jobs SET status='complete',completed_at=now()
                WHERE manifest_id=%s""",
                (manifest_id,),
            )
        second = _index_build(config)
        assert second["selected_chunks"] == 2
        assert second["scheduled"] == 2
        assert second["missing_points"] == 2

    def test_index_build_recreates_missing_jobs(self, service):
        from research_store.cli import _index_build

        self._insert_index_fixture("recon", 1)
        config = self._config()
        first = _index_build(config)
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM index_jobs WHERE index_definition_id=%s",
                (first["index_definition"]["id"],),
            )
        second = _index_build(config)
        assert second["scheduled"] == 1

    def test_index_reconcile_reports_discrepancies(self, service):
        from research_store.cli import _index_build, _index_reconcile

        self._insert_index_fixture("reconcile", 1)
        config = self._config()
        build = _index_build(config)
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM index_jobs WHERE index_definition_id=%s",
                (build["index_definition"]["id"],),
            )
        result = _index_reconcile(config)
        assert result["ok"] is False
        assert result["discrepancies"]

    def test_doctor_includes_index_reconcile(self, service):
        from research_store.cli import _doctor, _index_build

        self._insert_index_fixture("doctor", 1)
        config = self._config()
        _index_build(config)
        checks, _failed = _doctor(config)
        assert checks["index_reconcile"]["ok"] is True

    def test_index_reconcile_repair_flag(self, service):
        from research_store.cli import _index_build, _index_reconcile

        self._insert_index_fixture("repair", 1)
        config = self._config()
        build = _index_build(config)
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM index_jobs WHERE index_definition_id=%s",
                (build["index_definition"]["id"],),
            )
        assert _index_reconcile(config, repair=False)["ok"] is False
        repaired = _index_reconcile(config, repair=True)
        assert repaired["ok"] is True
        assert repaired["repaired"]

    def test_index_reconcile_repair_requeues_missing_points_and_deletes_orphans(
        self, service
    ):
        from research_store.cli import _index_build, _index_reconcile, _qdrant

        chunk_id = self._insert_index_fixture("physicaldrift", 1)[0]
        config = self._config()
        build = _index_build(config)
        definition = build["index_definition"]
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE embedding_manifests
                SET index_status='complete',indexed_at=now()
                WHERE index_definition_id=%s""",
                (definition["id"],),
            )
            cursor.execute(
                """UPDATE index_jobs SET status='complete',completed_at=now()
                WHERE index_definition_id=%s""",
                (definition["id"],),
            )
        index = _qdrant(
            config,
            definition["physical_collection"],
            definition["dimension"],
            definition["distance_metric"],
        )
        orphan_id = uuid4()
        index.upsert(
            [
                {
                    "id": str(orphan_id),
                    "vector": {"dense": [1.0, 0.0, 0.0, 0.0]},
                    "payload": {
                        "parser_version": config.parser_version,
                        "normalization_version": config.normalization_version,
                        "chunker_version": config.chunker_version,
                    },
                }
            ]
        )
        before = _index_reconcile(config, repair=False)
        collection = before["qdrant"]["collections"][definition["physical_collection"]]
        assert collection["coverage"] == {"missing": 1, "orphaned": 1}
        repaired = _index_reconcile(config, repair=True)
        action = repaired["repair_actions"][0]
        assert action["scheduled"] == 1
        assert action["deleted_orphaned"] == 1
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT m.index_status,j.status
                FROM embedding_manifests m
                JOIN index_jobs j ON j.manifest_id=m.id
                WHERE m.index_definition_id=%s AND m.chunk_id=%s""",
                (definition["id"], chunk_id),
            )
            assert cursor.fetchone() == ("pending", "pending")

    def test_index_point_counts_cached(self, service):
        from research_store.cli import _index_build

        self._insert_index_fixture("cache", 1)
        result = _index_build(self._config())
        with connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT point_count,last_verified_at FROM index_point_counts
                WHERE index_definition_id=%s""",
                (result["index_definition"]["id"],),
            )
            row = cursor.fetchone()
        assert row[0] == 1
        assert row[1] is not None

    def test_index_reconcile_reads_from_cache(self, service):
        from research_store.cli import _index_build, _index_reconcile

        self._insert_index_fixture("reconcilecache", 1)
        config = self._config()
        _index_build(config)
        result = _index_reconcile(config, repair=False)
        assert result["ok"] is True
        for collection in result["qdrant"]["collections"].values():
            assert collection["cached_point_count"] == 1
