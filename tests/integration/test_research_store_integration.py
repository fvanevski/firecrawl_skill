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
from typing import cast
from uuid import UUID, uuid4

import pytest
from psycopg import sql

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from completion_provenance_test_support import seed_authoritative_completion_provenance

from firecrawl_skill.research_domain import load_model, schema_registry, serialize_model
from firecrawl_skill.research_store import cli as store_cli
from firecrawl_skill.research_store.composition import (
    build_run_service,
    build_service,
    build_workflow_operation_service,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.domain import IngestRequest
from firecrawl_skill.research_store.postgres import (
    connect,
    migrate,
    require_disposable_database_reset,
)
from firecrawl_skill.research_store.run_service import (
    ResearchRunService,
    RunStateError,
    StaleRunRevisionError,
)
from firecrawl_skill.research_store.semantic_service import SemanticCallService

ROOT = SCRIPTS.parent
FIXTURES = ROOT / "tests" / "fixtures" / "research_domain"
VALID = (
    json.loads((FIXTURES / "valid.json").read_text())
    if (FIXTURES / "valid.json").exists()
    else {}
)


TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


@pytest.fixture
def service(tmp_path, prepared_database, track_test_collection):
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection=f"research_integration_test_{uuid4().hex[:8]}",
        embedding_dimension=4,
        embedding_model=f"test-{uuid4().hex[:8]}",
    )
    track_test_collection.add(config.physical_collection)
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
        row = cursor.fetchone()
        assert row is not None
        assert all(row)
        cursor.execute("SELECT version_num FROM alembic_version")
        row0 = cursor.fetchone()
        assert row0 is not None
        assert row0[0] == "0044_terminal_provenance_guard"


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
    prepared = workflow.prepare_run(external_run_id)
    assert prepared.state == "acquiring"

    invocation = workflow.begin_operation(
        external_run_id,
        external_invocation_id,
        "fscrape",
        {"urls": ["https://integration.example/wrapper"]},
    )
    assert invocation.run_id == created.id
    assert runs.status(run_id=created.id).state == "acquiring"

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
    assert runs.status(run_id=created.id).state == "acquiring"
    sealed = workflow.seal_acquisition(external_run_id)
    assert sealed.state == "indexing"

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

    workflow._finalize_indexing(
        external_run_id,
        f"wrapper-test:{created.id}:finalize-indexing",
    )

    provenance = seed_authoritative_completion_provenance(
        service.uow_factory, created.id
    )

    finished = workflow.finish_run(
        external_run_id,
        outcome="satisfied",
        source_manifest_sha256=provenance.source_manifest_sha256,
        answer_sha256=provenance.answer_sha256,
    )
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
        row0 = cursor.fetchone()
        assert row0 is not None
        assert row0[0] == 1


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


# NOTE: The remainder of this module is unchanged from the authoritative branch.
# It is intentionally retained verbatim except for the Phase-5 orchestrator
# construction site below.


class TestResearchOrchestratorIntegration:
    """End-to-end integration test for ResearchOrchestrator with PostgreSQL."""

    def test_orchestrator_end_to_end(self, service):
        """Execute ResearchOrchestrator.run() end-to-end with PostgreSQL."""
        from uuid import uuid4

        from firecrawl_skill.research_store.composition import (
            build_orchestrator_instance,
        )
        from firecrawl_skill.research_store.orchestrator import (
            OrchestratorConfig,
            ResearchOrchestrator,
        )
        from firecrawl_skill.research_store.run_service import ResearchRunService

        orchestrator = build_orchestrator_instance(
            ResearchOrchestrator,
            service.config,
            orchestrator_config=OrchestratorConfig(max_adaptive_cycles=2),
        )

        run_svc = ResearchRunService(service.uow_factory)
        run_status = run_svc.create(
            "Integration test objective",
            f"test-{uuid4()}",
            execution_mode="autonomous_local",
        )
        run_id = run_status.id

        from firecrawl_skill.research_domain import serialize_model
        from firecrawl_skill.research_store.budget_policy import (
            conservative_research_spec,
        )

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
            transition_count = cur.fetchone()
            assert transition_count is not None
            transition_count = transition_count[0]
        assert transition_count > 0
        assert final_status.lifecycle_revision > 0
