"""PostgreSQL-backed acceptance evidence for issue #310 research controller."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.acquisition.adapters.bounded_firecrawl import (
    BoundedFirecrawlSearchAdapter,
)
from firecrawl_skill.research_store.blob import ContentAddressedBlobStore
from firecrawl_skill.research_store.composition import (
    build_evidence_service,
    build_invocation_service,
    build_production_resumable_orchestrator,
    build_run_service,
    build_semantic_service,
    build_uow_factory,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.corpus_service import CorpusService
from firecrawl_skill.research_store.coverage_seed_service import (
    CompleteCoverageService,
)
from firecrawl_skill.research_store.domain import IngestRequest
from firecrawl_skill.research_store.invocation_service import InvocationRecord
from firecrawl_skill.research_store.parsing import get_registry
from firecrawl_skill.research_store.postgres import (
    connect,
    migrate,
    require_disposable_database_reset,
)
from firecrawl_skill.research_store.research_controller import (
    ControllerPolicy,
    ResearchWorkflowController,
)
from firecrawl_skill.research_store.research_controller_contract import (
    DISPOSITION_BLOCKED,
    DISPOSITION_COMPLETED,
    DISPOSITION_FAILED,
    DISPOSITION_OPERATOR,
    DISPOSITION_PARTIAL,
    WorkflowDirective,
)

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="requires repository-sanctioned disposable PostgreSQL",
)
OBJECTIVE = "issue310 retained postgres controller authority"


@pytest.fixture(scope="module", autouse=True)
def prepared_database() -> None:
    require_disposable_database_reset(
        TEST_DSN,
        os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", ""),
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    migrate(TEST_DSN)


@pytest.fixture
def controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ResearchWorkflowController, CorpusService, list[str]]:
    monkeypatch.setenv("FIRECRAWL_RELEASE_DETERMINISTIC_FIXTURES", "1")
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        embedding_model=f"issue310-{uuid4().hex[:8]}",
        embedding_revision="test",
        embedding_dimension=4,
    )
    run_service = build_run_service(config)
    corpus = CorpusService(
        config,
        build_uow_factory(config),
        ContentAddressedBlobStore(config.blob_root),
        parser_registry=get_registry(),
    )
    coverage = CompleteCoverageService(run_service.uow_factory)
    provider_calls: list[str] = []

    def forbidden_provider_search(
        self: Any,
        query_text: str,
        **_kwargs: Any,
    ) -> Any:
        provider_calls.append(query_text)
        raise AssertionError("retained-first controller invoked Firecrawl provider")

    monkeypatch.setattr(
        BoundedFirecrawlSearchAdapter,
        "search",
        forbidden_provider_search,
    )

    workflow = ResearchWorkflowController(
        config=config,
        run_service=run_service,
        invocation_service=build_invocation_service(config),
        corpus_service=corpus,
        coverage_service=coverage,
        evidence_service=build_evidence_service(config),
        semantic_service=build_semantic_service(config),
        orchestrator_factory=lambda orchestrator_config: (
            build_production_resumable_orchestrator(
                config,
                orchestrator_config=orchestrator_config,
            )
        ),
        controller_config=None,
    )
    return workflow, corpus, provider_calls


def _seed_retained(corpus: CorpusService) -> None:
    corpus.ingest(
        IngestRequest(
            requested_url="https://issue310.example/retained",
            content=(
                b"Issue310 retained postgres controller authority is established "
                b"by this durable retained corpus evidence."
            ),
            title="Issue310 retained controller authority",
        )
    )


def _planning_invocations(
    workflow: ResearchWorkflowController,
    public_run_id: str,
) -> list[InvocationRecord]:
    status = workflow.run_service.status(external_id=public_run_id)
    return workflow.invocation_service.list_invocations(
        status.id,
        operation="fresearch_planning",
    )


def _ambiguous_interpretation(objective: str) -> SimpleNamespace:
    return SimpleNamespace(
        value={
            "schema_version": "smart-objective-intent-v2",
            "objective": objective,
            "research_questions": [objective],
            "entities": [],
            "jurisdictions": [],
            "user_constraints": [],
            "exact_source_requirements": [],
            "temporal": {
                "kind": "none",
                "relative_quantity": None,
                "relative_unit": None,
                "freshness_basis": None,
                "temporal_basis": "none",
                "publication_start": None,
                "publication_end": None,
                "event_start": None,
                "event_end": None,
                "as_of": None,
                "uncertainty": "ambiguous",
                "rationale": "latest has no explicit temporal horizon",
            },
            "assumptions": [],
            "ambiguities": ["latest has no explicit temporal horizon"],
        },
        error=None,
        provenance={"authority": "issue386-postgres-regression"},
        semantic_call_id=uuid4(),
        artifact_ids=(uuid4(),),
    )


def test_ambiguous_objective_requires_durable_action_and_resumes_same_run(
    controller: tuple[ResearchWorkflowController, CorpusService, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import firecrawl_skill.research_store.research_controller as controller_module

    workflow, corpus, provider_calls = controller
    _seed_retained(corpus)
    provider_calls.clear()
    objective = f"issue386 latest retained semantic authority {uuid4().hex}"
    monkeypatch.setattr(
        controller_module,
        "interpret_smart_objective",
        lambda **kwargs: _ambiguous_interpretation(str(kwargs["objective"])),
    )

    first = workflow.run(objective, execution_mode="deterministic_debug")

    assert isinstance(first, WorkflowDirective)
    assert first.disposition == DISPOSITION_OPERATOR
    assert first.action_kind == "semantic_resolution_required"
    assert first.action_id is not None
    assert provider_calls == []
    parent = workflow.run_service.status(external_id=first.run_id)
    assert parent.state == "created"
    invocations = _planning_invocations(workflow, first.run_id)
    assert len(invocations) == 1
    assert invocations[0].status == "running"

    public = workflow.action(first.action_id)
    assert public["kind"] == "semantic_resolution_required"
    assert public["status"] == "pending"
    assert public["public_payload"]["resolution_type"] == "accept_proposed_intent"
    assert public["public_payload"]["objective"] == objective
    assert public["public_payload"]["ambiguity_diagnostics"] == [
        "latest has no explicit temporal horizon"
    ]
    assert public["public_payload"]["material_scope_change_requires_fork"] is True
    serialized = json.dumps(public, sort_keys=True)
    for forbidden in (
        "semantic_call_id",
        "artifact_ids",
        "planning_invocation_id",
        "authority_fingerprint",
        "lifecycle_revision",
        "research_spec_id",
    ):
        assert forbidden not in serialized

    with workflow.run_service.uow_factory() as uow:
        stored = uow.operator_actions.get_action(external_action_id=first.action_id)
        internal = dict((stored.get("creation_payload") or {}).get("internal") or {})
    assert internal["objective"] == objective
    assert internal["planning_invocation_id"] == invocations[0].external_invocation_id
    semantic_call_id = internal["semantic_provenance"]["semantic_call_id"]
    assert semantic_call_id

    restarted = ResearchWorkflowController(
        config=workflow.config,
        run_service=workflow.run_service,
        invocation_service=workflow.invocation_service,
        corpus_service=workflow.corpus_service,
        coverage_service=workflow.coverage_service,
        evidence_service=workflow.evidence_service,
        semantic_service=workflow.semantic_service,
        orchestrator_factory=workflow.orchestrator_factory,
        controller_config=workflow.controller_config,
        clock=workflow.clock,
    )
    assert restarted.action(first.action_id) == public
    monkeypatch.setattr(
        controller_module,
        "interpret_smart_objective",
        lambda **_kwargs: pytest.fail(
            "semantic interpreter must not rerun after persisted human resolution"
        ),
    )

    reason = "human accepts the exact persisted semantic proposal"
    resolved = restarted.resolve(
        first.action_id,
        accept_proposed_intent=True,
        reason=reason,
        authorized_by="issue386-operator",
    )
    assert resolved.run_id == first.run_id
    assert resolved.disposition == DISPOSITION_COMPLETED
    assert resolved.objective_satisfied is True
    assert provider_calls == []

    replayed = restarted.resolve(
        first.action_id,
        accept_proposed_intent=True,
        reason=reason,
        authorized_by="issue386-operator",
    )
    assert replayed.run_id == first.run_id
    assert replayed.disposition == DISPOSITION_COMPLETED
    assert provider_calls == []

    final_action = restarted.action(first.action_id)
    assert final_action["status"] == "resolved"
    assert final_action["resolution"]["payload"] == {
        "decision": "accepted_proposed_intent"
    }
    final_invocations = _planning_invocations(restarted, first.run_id)
    assert len(final_invocations) == 1
    assert final_invocations[0].id == invocations[0].id
    assert final_invocations[0].status == "complete"

    status = restarted.run_service.status(external_id=first.run_id)
    with restarted.run_service.uow_factory() as uow:
        spec = uow.runs.get_research_spec(status.id)
        events = uow.runs.list_events(
            status.id,
            event_type="planning.provenance_recorded",
            limit=2,
            offset=0,
        )
    assert spec is not None
    assert (spec.get("payload") or {}).get("ambiguities") == []
    assert len(events) == 1
    objective_intent = (events[0].get("payload") or {}).get("objective_intent") or {}
    assert objective_intent["semantic_call_id"] == semantic_call_id
    assert objective_intent["operator_resolution"] == {
        "action_id": first.action_id,
        "decision": "accepted_proposed_intent",
        "authorized_by": "issue386-operator",
        "reason": reason,
    }


def test_unsupported_semantic_intent_stays_fail_closed_without_action(
    controller: tuple[ResearchWorkflowController, CorpusService, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import firecrawl_skill.research_store.research_controller as controller_module

    workflow, _corpus, provider_calls = controller
    provider_calls.clear()
    monkeypatch.setattr(
        controller_module,
        "interpret_smart_objective",
        lambda **_kwargs: SimpleNamespace(
            value=None,
            error=(
                "semantic objective intent is unsupported and cannot be represented "
                "by the public resolution contract"
            ),
            provenance={"authority": "issue386-postgres-regression"},
            semantic_call_id=uuid4(),
            artifact_ids=(uuid4(),),
        ),
    )

    result = workflow.run(
        f"issue386 unsupported semantic intent {uuid4().hex}",
        execution_mode="deterministic_debug",
    )

    assert result.disposition == DISPOSITION_FAILED
    assert result.action_id is None
    assert provider_calls == []
    status = workflow.run_service.status(external_id=result.run_id)
    assert status.state == "failed"
    with workflow.run_service.uow_factory() as uow:
        assert uow.operator_actions.pending_for_run(status.id) is None


def test_semantic_scope_change_uses_fork_and_preserves_parent_authority(
    controller: tuple[ResearchWorkflowController, CorpusService, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import firecrawl_skill.research_store.research_controller as controller_module

    workflow, _corpus, provider_calls = controller
    provider_calls.clear()
    parent_objective = f"issue386 latest ambiguous parent {uuid4().hex}"
    monkeypatch.setattr(
        controller_module,
        "interpret_smart_objective",
        lambda **kwargs: _ambiguous_interpretation(str(kwargs["objective"])),
    )
    first = workflow.run(
        parent_objective,
        execution_mode="deterministic_debug",
        delivery_mode="host_handoff",
    )
    assert first.disposition == DISPOSITION_OPERATOR
    assert first.action_id is not None
    parent_before = workflow.run_service.status(external_id=first.run_id)
    revised = f"issue386 materially revised child {uuid4().hex}"

    child_result = workflow.fork(
        first.action_id,
        revised,
        reason="human selected a materially different scope",
        authorized_by="issue386-operator",
    )

    assert child_result.run_id != first.run_id
    assert child_result.disposition == DISPOSITION_OPERATOR
    assert child_result.action_kind == "semantic_resolution_required"
    parent_after = workflow.run_service.status(external_id=first.run_id)
    child = workflow.run_service.status(external_id=child_result.run_id)
    assert parent_after.objective == parent_objective
    assert parent_after.state == parent_before.state
    assert parent_after.lifecycle_revision == parent_before.lifecycle_revision
    assert child.objective == revised
    assert provider_calls == []

    with workflow.run_service.uow_factory() as uow:
        lineage = uow.operator_actions.lineage_for_child(child.id)
        policy_events = uow.runs.list_events(
            child.id,
            event_type="controller.policy_recorded",
            limit=2,
            offset=0,
        )
    assert lineage is not None
    assert lineage["parent_run_id"] == parent_after.id
    assert lineage["operator_action_id"] is not None
    assert len(policy_events) == 1
    assert policy_events[0]["payload"]["delivery_mode"] == "host_handoff"
    assert workflow.action(first.action_id)["resolution"]["payload"] == {
        "decision": "forked",
        "child_run_id": child_result.run_id,
        "parent_run_id": first.run_id,
        "child_objective": revised,
    }

    parent_recheck = workflow.continue_run(first.run_id)
    assert isinstance(parent_recheck, WorkflowDirective)
    assert parent_recheck.disposition == DISPOSITION_BLOCKED
    assert parent_recheck.action_kind == "follow_forked_child"
    assert any(child_result.run_id in item for item in parent_recheck.diagnostics)
    parent_final = workflow.run_service.status(external_id=first.run_id)
    assert parent_final.state == parent_before.state
    assert parent_final.lifecycle_revision == parent_before.lifecycle_revision


def test_retained_sufficient_completes_with_zero_provider_calls(
    controller: tuple[ResearchWorkflowController, CorpusService, list[str]],
) -> None:
    workflow, corpus, provider_calls = controller
    _seed_retained(corpus)

    result = workflow.run(
        OBJECTIVE,
        execution_mode="deterministic_debug",
    )

    assert result.disposition == DISPOSITION_COMPLETED
    assert result.result_ready is True
    assert result.objective_satisfied is True
    assert result.handoff_ready is True
    assert provider_calls == []

    invocations = _planning_invocations(workflow, result.run_id)
    assert len(invocations) == 1
    assert invocations[0].status == "complete"
    assert (invocations[0].external_invocation_id or "").startswith("fc_")
    assert invocations[0].operation == "fresearch_planning"
    assert invocations[0].output is not None
    assert invocations[0].output.get("schema_version") == "fresearch-planning-result-v1"

    status = workflow.run_service.status(external_id=result.run_id)
    seal = workflow.retained_completion.get_active_seal(status.id)
    assert seal is not None
    assert seal.status == "sealed"
    assert seal.expected_asset_count >= 1
    assert seal.expected_chunk_count >= 1
    assert len(seal.members) == seal.expected_asset_count

    with workflow.run_service.uow_factory() as uow, uow.connection.cursor() as cur:
        cur.execute(
            """SELECT invocation_id,status FROM semantic_calls
               WHERE run_id=%s AND idempotency_key=%s""",
            (status.id, f"smart:objective-intent:{status.id}:r1"),
        )
        semantic_rows = cur.fetchall()
    assert semantic_rows == [(invocations[0].id, "complete")]


def test_retained_only_insufficient_is_partial_with_zero_provider_calls(
    controller: tuple[ResearchWorkflowController, CorpusService, list[str]],
) -> None:
    workflow, _corpus, provider_calls = controller
    provider_calls.clear()

    result = workflow.run(
        f"absent retained evidence {uuid4().hex}",
        retained_only=True,
        execution_mode="deterministic_debug",
    )

    assert result.disposition == DISPOSITION_PARTIAL
    assert result.result_ready is True
    assert result.objective_satisfied is False
    assert provider_calls == []


def test_low_level_run_without_controller_policy_returns_blocked_directive(
    controller: tuple[ResearchWorkflowController, CorpusService, list[str]],
) -> None:
    workflow, _corpus, provider_calls = controller
    provider_calls.clear()
    status = workflow.run_service.create(
        "issue310 low-level run without controller policy",
        f"fr_{uuid4().hex}",
        execution_mode="deterministic_debug",
        actor_type="test",
        actor_identifier="issue310-review-regression",
    )

    directive = workflow.continue_run(status.external_id or "")

    assert isinstance(directive, WorkflowDirective)
    assert directive.disposition == DISPOSITION_BLOCKED
    assert directive.action_kind == "inspect_blocker"
    assert directive.lifecycle_state == "created"
    assert directive.lifecycle_revision == status.lifecycle_revision
    assert directive.result_ready is False
    assert any(
        "no canonical controller policy" in item for item in directive.diagnostics
    )
    assert provider_calls == []


@pytest.mark.parametrize(
    "restart_state",
    ["created", "planning", "corpus_review", "retrieving"],
)
def test_restart_from_early_automatic_transitions_uses_persisted_authority(
    controller: tuple[ResearchWorkflowController, CorpusService, list[str]],
    restart_state: str,
) -> None:
    workflow, corpus, provider_calls = controller
    _seed_retained(corpus)
    provider_calls.clear()

    status = workflow.run_service.create(
        OBJECTIVE,
        f"fr_{uuid4().hex}",
        execution_mode="deterministic_debug",
        actor_type="controller",
        actor_identifier="ResearchWorkflowController",
    )
    policy = ControllerPolicy(
        retained_only=False,
        evaluated_at=workflow.clock(),
    )
    workflow._record_policy(status, policy)
    bundle = workflow._initialize_planning(status, policy)

    if restart_state in {"planning", "corpus_review", "retrieving"}:
        status = workflow._transition(
            status,
            "planning",
            key=f"test:planning:{status.id}",
            reason="simulate persisted automatic transition",
        )
    if restart_state in {"corpus_review", "retrieving"}:
        status = workflow._transition(
            status,
            "corpus_review",
            key=f"test:corpus-review:{status.id}",
            reason="simulate persisted automatic transition",
        )
    if restart_state == "retrieving":
        status = workflow._enter_retained_review(status, bundle)

    result = workflow.continue_run(status.external_id or "")
    assert result.disposition == DISPOSITION_COMPLETED
    assert provider_calls == []
    invocations = _planning_invocations(workflow, result.run_id)
    assert len(invocations) == 1
    assert invocations[0].status == "complete"


def test_restart_after_planning_persistence_reuses_running_invocation(
    controller: tuple[ResearchWorkflowController, CorpusService, list[str]],
) -> None:
    workflow, corpus, provider_calls = controller
    _seed_retained(corpus)
    provider_calls.clear()

    status = workflow.run_service.create(
        OBJECTIVE,
        f"fr_{uuid4().hex}",
        execution_mode="deterministic_debug",
        actor_type="controller",
        actor_identifier="ResearchWorkflowController",
    )
    policy = ControllerPolicy(False, workflow.clock())
    workflow._record_policy(status, policy)
    invocation = workflow._begin_planning_invocation(status, policy)
    workflow._persist_planning(status, policy, invocation)

    before = _planning_invocations(workflow, status.external_id or "")
    assert len(before) == 1
    assert before[0].id == invocation.id
    assert before[0].status == "running"

    result = workflow.continue_run(status.external_id or "")

    assert result.disposition == DISPOSITION_COMPLETED
    assert provider_calls == []
    after = _planning_invocations(workflow, result.run_id)
    assert len(after) == 1
    assert after[0].id == invocation.id
    assert after[0].status == "complete"


def test_restart_from_retained_coverage_decision_is_deterministic(
    controller: tuple[ResearchWorkflowController, CorpusService, list[str]],
) -> None:
    workflow, corpus, provider_calls = controller
    _seed_retained(corpus)
    provider_calls.clear()

    status = workflow.run_service.create(
        OBJECTIVE,
        f"fr_{uuid4().hex}",
        execution_mode="deterministic_debug",
        actor_type="controller",
        actor_identifier="ResearchWorkflowController",
    )
    policy = ControllerPolicy(False, workflow.clock())
    workflow._record_policy(status, policy)
    bundle = workflow._initialize_planning(status, policy)
    status = workflow._transition(
        status,
        "planning",
        key=f"test:planning:{status.id}",
        reason="simulate automatic transition",
    )
    status = workflow._transition(
        status,
        "corpus_review",
        key=f"test:corpus-review:{status.id}",
        reason="simulate automatic transition",
    )
    status = workflow._enter_retained_review(status, bundle)
    evaluation = workflow.retained_review.evaluate(
        status,
        bundle,
        evaluated_at=policy.evaluated_at,
    )
    assert evaluation.outcome == "sufficient"
    status = workflow._transition(
        status,
        "coverage_review",
        key=f"test:coverage-review:{status.id}",
        reason="simulate crash after retained evaluation",
    )

    result = workflow.continue_run(status.external_id or "")
    assert result.disposition == DISPOSITION_COMPLETED
    assert provider_calls == []


def test_restart_after_retained_membership_seal_reuses_exact_authority(
    controller: tuple[ResearchWorkflowController, CorpusService, list[str]],
) -> None:
    workflow, corpus, provider_calls = controller
    _seed_retained(corpus)
    provider_calls.clear()

    status = workflow.run_service.create(
        OBJECTIVE,
        f"fr_{uuid4().hex}",
        execution_mode="deterministic_debug",
        actor_type="controller",
        actor_identifier="ResearchWorkflowController",
    )
    policy = ControllerPolicy(False, workflow.clock())
    workflow._record_policy(status, policy)
    bundle = workflow._initialize_planning(status, policy)
    status = workflow._transition(
        status,
        "planning",
        key=f"test:seal-restart-planning:{status.id}",
        reason="simulate automatic transition",
    )
    status = workflow._transition(
        status,
        "corpus_review",
        key=f"test:seal-restart-corpus-review:{status.id}",
        reason="simulate automatic transition",
    )
    status = workflow._enter_retained_review(status, bundle)
    evaluation = workflow.retained_review.evaluate(
        status,
        bundle,
        evaluated_at=policy.evaluated_at,
    )
    assert evaluation.outcome == "sufficient"
    status = workflow._transition(
        status,
        "coverage_review",
        key=f"test:seal-restart-coverage-review:{status.id}",
        reason="simulate automatic transition",
    )

    assert workflow._prepare_retained_completion_membership(status) is None
    before = workflow.retained_completion.get_active_seal(status.id)
    assert before is not None
    assert workflow.run_service.status(run_id=status.id).state == "coverage_review"

    result = workflow.continue_run(status.external_id or "")

    assert result.disposition == DISPOSITION_COMPLETED
    assert provider_calls == []
    after = workflow.retained_completion.get_active_seal(status.id)
    assert after is not None
    assert after.id == before.id
    assert after.seal_revision == before.seal_revision
    assert after.lifecycle_revision == before.lifecycle_revision
    assert after.membership_sha256 == before.membership_sha256
    assert after.members == before.members


def test_no_controller_run_exposes_internal_identity_in_public_result(
    controller: tuple[ResearchWorkflowController, CorpusService, list[str]],
) -> None:
    workflow, _corpus, _provider_calls = controller
    latest = workflow.run(
        f"absent retained evidence {uuid4().hex}",
        retained_only=True,
        execution_mode="deterministic_debug",
    )
    payload = latest.to_dict()
    assert payload["run_id"].startswith("fr_")
    for forbidden in (
        "research_spec_id",
        "search_plan_id",
        "invocation_id",
        "membership_fingerprint",
        "candidate_budget_check_id",
    ):
        assert forbidden not in payload
