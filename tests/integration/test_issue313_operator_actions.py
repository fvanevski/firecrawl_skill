from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from asset_promotion_test_support import TEST_DSN, _insert_candidate, _seed_retained_assets

from firecrawl_skill.research_store.acquisition.candidate_ranking import CandidateBudget
from firecrawl_skill.research_store.asset_promotion_service import AssetPromotionService
from firecrawl_skill.research_store.candidate_budget_outcomes import (
    CandidateBudgetHardRejected,
    CandidateBudgetOverrideRequired,
)
from firecrawl_skill.research_store.candidate_policy_service import CandidatePolicyError
from firecrawl_skill.research_store.composition import build_run_service
from firecrawl_skill.research_store.operator_action_service import (
    ACTION_CURATION,
    OPERATOR_ACTION_POLICY_VERSION,
    OperatorActionConflictError,
    OperatorActionService,
    StaleOperatorActionError,
)
from firecrawl_skill.research_store.postgres_operator_actions import (
    PostgresOperatorActionRepository,
)
from firecrawl_skill.research_store.resume_state_repository import (
    PostgresResumeStateReader,
)

pytest_plugins = ("asset_promotion_test_support",)
pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="requires repository-sanctioned disposable PostgreSQL",
)


def _operator_service(
    promotion: AssetPromotionService,
    runs,
) -> OperatorActionService:
    return OperatorActionService(
        runs.uow_factory,
        candidate_policy=promotion.candidate_policy_service,
        promotion_service=promotion,
    )


def _soft_budget_boundary(promotion_config):
    _corpus, runs, status, _manifest = _seed_retained_assets(promotion_config)
    budget = CandidateBudget(
        max_per_asset_contribution_chunks=0,
        max_generic_page_share=1.0,
    )
    promotion = AssetPromotionService(runs.uow_factory, candidate_budget=budget)
    with pytest.raises(CandidateBudgetOverrideRequired) as exc_info:
        promotion.prepare_for_indexing(
            status.id,
            lifecycle_revision=status.lifecycle_revision,
        )
    actions = _operator_service(promotion, runs)
    action = actions.ensure_budget_action(status, exc_info.value.context)
    return runs, status, promotion, actions, action


def _curation_action(promotion_config, count: int = 2):
    _corpus, runs, status, _manifest = _seed_retained_assets(
        promotion_config,
        count=count,
    )
    promotion = AssetPromotionService(runs.uow_factory)
    actions = _operator_service(promotion, runs)
    action = actions.ensure_curation_action(status)
    assert action.kind == ACTION_CURATION
    census = list(action.creation_payload["internal"]["census"])
    return runs, status, promotion, actions, action, census


def _seed_scope_action(promotion_config, *, curated: bool = True):
    runs = build_run_service(promotion_config)
    objective = f"issue313 temporal parent {uuid4().hex}"
    status = runs.create(
        objective,
        f"fr_{uuid4().hex}",
        execution_mode="deterministic_debug",
        actor_type="controller",
        actor_identifier="ResearchWorkflowController",
    )
    with runs.uow_factory() as uow:
        uow.runs.append_event(
            status.id,
            "controller.policy_recorded",
            "controller",
            f"issue313:policy:{status.id}",
            actor_identifier="ResearchWorkflowController",
            payload={
                "schema_version": "research-controller-policy-v2",
                "retained_only": False,
                "curated": curated,
                "evaluated_at": "2026-08-25T12:00:00+00:00",
            },
        )
        uow.runs.record_research_spec(
            status.id,
            spec_revision=1,
            schema_name="research_spec",
            schema_version=1,
            payload={
                "schema_version": "research-spec-v1",
                "objective": objective,
            },
            idempotency_key=f"issue313:spec:{status.id}",
        )
        uow.commit()

    for next_state in ("planning", "corpus_review", "acquiring", "coverage_review"):
        current = runs.status(run_id=status.id)
        runs.transition(
            status.id,
            next_state,
            expected_revision=current.lifecycle_revision,
            idempotency_key=f"issue313:transition:{status.id}:{next_state}",
            actor_type="controller",
            actor_identifier="ResearchWorkflowController",
        )
    status = runs.status(run_id=status.id)
    gap = {
        "kind": "temporal_coverage_gap",
        "coverage_revision": 1,
        "reason": "authoritative publication interval remains unsatisfied",
    }
    with runs.uow_factory() as uow:
        uow.runs.append_event(
            status.id,
            "evidence.temporal_coverage_gap",
            "orchestrator",
            f"issue313:temporal-gap:{status.id}",
            actor_identifier="ResumableResearchOrchestrator",
            payload={"temporal_coverage_gap": gap},
        )
        uow.commit()
    actions = OperatorActionService(runs.uow_factory)
    action = actions.ensure_scope_action(status, gap)
    return runs, status, actions, action


def _stages(promotion: AssetPromotionService, run_id: UUID) -> dict[str, str]:
    return {
        str(item["id"]): str(item["current_stage"])
        for item in promotion.list_assets(run_id)
        if item.get("id") is not None
    }


def test_budget_action_hides_generated_parameters_and_resumes_exact_check(
    promotion_config,
) -> None:
    _runs, status, promotion, actions, action = _soft_budget_boundary(
        promotion_config
    )

    public = action.to_public_dict()
    assert public["action_id"].startswith("oa_")
    serialized = json.dumps(public, sort_keys=True, default=str)
    for forbidden in (
        "check_id",
        "soft_limits",
        "scope_fingerprint",
        "lifecycle_revision",
    ):
        assert forbidden not in serialized

    reason = "human accepts this exact soft corpus-budget exception"
    resolved = actions.approve(
        action.action_id,
        reason=reason,
        authorized_by="issue313-operator",
    )
    assert resolved.status == "resolved"
    assert resolved.resolution_payload == {"decision": "approved"}
    replayed = actions.approve(
        action.action_id,
        reason=reason,
        authorized_by="issue313-operator",
    )
    assert replayed.resolution_id == resolved.resolution_id

    restarted = _operator_service(promotion, _runs)
    assert restarted.describe(action.action_id).status == "resolved"

    seal = promotion.prepare_for_indexing(
        status.id,
        lifecycle_revision=status.lifecycle_revision,
    )
    assert seal.status == "sealed"
    assert seal.expected_asset_count == 1


def test_hard_budget_violation_never_creates_approvable_action(
    promotion_config,
) -> None:
    _corpus, runs, status, _manifest = _seed_retained_assets(promotion_config)
    budget = CandidateBudget(max_chunks=0, max_generic_page_share=1.0)
    promotion = AssetPromotionService(runs.uow_factory, candidate_budget=budget)
    with pytest.raises(CandidateBudgetHardRejected) as exc_info:
        promotion.prepare_for_indexing(
            status.id,
            lifecycle_revision=status.lifecycle_revision,
        )

    actions = _operator_service(promotion, runs)
    with pytest.raises(CandidatePolicyError, match="not approvable"):
        actions.ensure_budget_action(status, exc_info.value.context)
    assert actions.active_for_run(status) is None


def test_conflicting_concurrent_budget_resolution_has_one_winner(
    promotion_config,
) -> None:
    runs, _status, promotion, _actions, action = _soft_budget_boundary(
        promotion_config
    )
    barrier = Barrier(2)

    def resolve(reason: str):
        service = _operator_service(promotion, runs)
        barrier.wait()
        try:
            return service.approve(
                action.action_id,
                reason=reason,
                authorized_by="issue313-operator",
            ).status
        except Exception as exc:  # noqa: BLE001 - exact loser type asserted below.
            return type(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(resolve, ("approval-a", "approval-b")))

    assert results.count("resolved") == 1
    assert results.count(OperatorActionConflictError) == 1


def test_curation_census_ignores_unextracted_discovered_candidates(
    promotion_config,
) -> None:
    _corpus, runs, status, _manifest = _seed_retained_assets(
        promotion_config,
        count=2,
    )
    _insert_candidate(status.id, "unextracted-candidate")
    promotion = AssetPromotionService(runs.uow_factory)
    actions = _operator_service(promotion, runs)

    action = actions.ensure_curation_action(status)
    census = list(action.creation_payload["internal"]["census"])

    assert len(census) == 2
    assert {item["current_stage"] for item in census} == {"retained"}
    assert all(item["snapshot_id"] for item in census)


def test_curation_restart_reconstructs_pending_action_and_filters_rejected_resume_assets(
    promotion_config,
) -> None:
    runs, status, promotion, _actions, action, census = _curation_action(
        promotion_config
    )
    restarted = _operator_service(promotion, runs)
    active = restarted.active_for_run(status)
    assert active is not None
    assert active.action_id == action.action_id

    retained_subject = UUID(str(census[0]["subject_id"]))
    retained_snapshot = str(census[0]["snapshot_id"])
    reason = "retain the single authoritative curated source"
    resolved = restarted.curate(
        action.action_id,
        retain_subject_ids=[retained_subject],
        reject_rest=True,
        reason=reason,
        authorized_by="issue313-operator",
    )
    assert resolved.status == "resolved"
    replayed = restarted.curate(
        action.action_id,
        retain_subject_ids=[retained_subject],
        reject_rest=True,
        reason=reason,
        authorized_by="issue313-operator",
    )
    assert replayed.resolution_id == resolved.resolution_id

    stages = _stages(promotion, status.id)
    assert stages[str(retained_subject)] == "retained"
    assert set(stages.values()) == {"retained", "rejected"}

    replay = PostgresResumeStateReader(runs.uow_factory).assets(status.id)
    assert {item["snapshot_id"] for item in replay} == {retained_snapshot}


def test_curation_resolution_rolls_back_subject_mutations_if_action_commit_fails(
    promotion_config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runs, status, promotion, actions, action, census = _curation_action(
        promotion_config
    )
    before = _stages(promotion, status.id)

    def fail_resolution(self, *args, **kwargs):
        raise RuntimeError("injected operator-action resolution failure")

    monkeypatch.setattr(
        PostgresOperatorActionRepository,
        "finish_action",
        fail_resolution,
    )
    with pytest.raises(RuntimeError, match="injected operator-action"):
        actions.curate(
            action.action_id,
            retain_subject_ids=[UUID(str(census[0]["subject_id"]))],
            reject_rest=True,
            reason="exercise outer transaction rollback",
            authorized_by="issue313-operator",
        )

    assert _stages(promotion, status.id) == before
    assert actions.describe(action.action_id).status == "pending"


def test_curation_membership_fingerprint_change_fails_closed_and_supersedes(
    promotion_config,
) -> None:
    _runs, status, promotion, actions, action, census = _curation_action(
        promotion_config
    )
    changed_subject = UUID(str(census[-1]["subject_id"]))
    promotion.reject(
        changed_subject,
        expected_lifecycle_revision=status.lifecycle_revision,
        expected_run_id=status.id,
        actor_type="operator",
        actor_identifier="issue313-external-change",
        policy_version=OPERATOR_ACTION_POLICY_VERSION,
        reason_code="test_membership_change",
        reason="change exact curation membership before action resolution",
    )

    with pytest.raises(StaleOperatorActionError, match="membership changed"):
        actions.curate(
            action.action_id,
            retain_subject_ids=[UUID(str(census[0]["subject_id"]))],
            reject_rest=True,
            reason="must fail against stale census",
            authorized_by="issue313-operator",
        )

    assert actions.active_for_run(status) is None
    assert actions.describe(action.action_id).status == "superseded"


def test_curation_lifecycle_change_fails_closed(
    promotion_config,
) -> None:
    runs, status, promotion, actions, action, census = _curation_action(
        promotion_config
    )
    runs.transition(
        status.id,
        "coverage_review",
        expected_revision=status.lifecycle_revision,
        idempotency_key=f"issue313:stale-lifecycle:{status.id}",
        actor_type="controller",
    )
    latest = runs.status(run_id=status.id)

    with pytest.raises(StaleOperatorActionError, match="lifecycle revision is stale"):
        actions.curate(
            action.action_id,
            retain_subject_ids=[UUID(str(census[0]["subject_id"]))],
            reject_rest=True,
            reason="must fail against stale lifecycle",
            authorized_by="issue313-operator",
        )

    assert actions.active_for_run(latest) is None
    assert actions.describe(action.action_id).status == "superseded"
    assert set(_stages(promotion, status.id).values()) == {"retained"}


def test_resolved_curation_policy_version_change_fails_closed(
    promotion_config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import firecrawl_skill.research_store.operator_action_service as action_module

    _runs, status, _promotion, actions, action, census = _curation_action(
        promotion_config
    )
    actions.curate(
        action.action_id,
        retain_subject_ids=[UUID(str(census[0]["subject_id"]))],
        reject_rest=True,
        reason="establish resolved curation authority",
        authorized_by="issue313-operator",
    )
    monkeypatch.setattr(
        action_module,
        "OPERATOR_ACTION_POLICY_VERSION",
        "operator-action-policy-v2",
    )

    with pytest.raises(
        action_module.OperatorActionError,
        match="resolved curation authority uses a stale",
    ):
        actions.curation_completed(status)


def test_operator_policy_version_change_supersedes_pending_action(
    promotion_config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import firecrawl_skill.research_store.operator_action_service as action_module

    _runs, status, _promotion, actions, action, _census = _curation_action(
        promotion_config
    )
    monkeypatch.setattr(
        action_module,
        "OPERATOR_ACTION_POLICY_VERSION",
        "operator-action-policy-v2",
    )

    assert actions.active_for_run(status) is None
    superseded = actions.describe(action.action_id)
    assert superseded.status == "superseded"
    assert "policy version is stale" in str(superseded.resolution_reason)


def test_material_scope_fork_preserves_parent_and_records_explicit_lineage(
    promotion_config,
) -> None:
    runs, parent, actions, action = _seed_scope_action(
        promotion_config,
        curated=True,
    )
    with runs.uow_factory() as uow:
        parent_spec_before = uow.runs.get_research_spec(parent.id)

    revised_objective = "issue313 revised temporal publication interval"
    reason = "human materially revised the authoritative temporal scope"
    resolved, child_external_id = actions.fork(
        action.action_id,
        revised_objective,
        reason=reason,
        authorized_by="issue313-operator",
    )
    assert resolved.status == "resolved"
    replayed, replayed_child_id = actions.fork(
        action.action_id,
        revised_objective,
        reason=reason,
        authorized_by="issue313-operator",
    )
    assert replayed.resolution_id == resolved.resolution_id
    assert replayed_child_id == child_external_id
    assert child_external_id.startswith("fr_")
    assert child_external_id != parent.external_id

    parent_after = runs.status(run_id=parent.id)
    assert parent_after.objective == parent.objective
    assert parent_after.state == parent.state
    assert parent_after.lifecycle_revision == parent.lifecycle_revision

    child = runs.status(external_id=child_external_id)
    assert child.id != parent.id
    assert child.objective == revised_objective
    assert child.state == "created"

    with runs.uow_factory() as uow:
        parent_spec_after = uow.runs.get_research_spec(parent.id)
        lineage = uow.operator_actions.lineage_for_child(child.id)
        policy_events = uow.runs.list_events(
            child.id,
            event_type="controller.policy_recorded",
            limit=2,
            offset=0,
        )
    assert parent_spec_after == parent_spec_before
    assert lineage is not None
    assert UUID(str(lineage["parent_run_id"])) == parent.id
    assert UUID(str(lineage["operator_action_id"])) == action.id
    assert lineage["parent_spec_id"] == UUID(str(parent_spec_before["id"]))
    assert int(lineage["parent_spec_revision"]) == 1
    assert lineage["child_objective"] == child.objective
    assert len(policy_events) == 1
    assert policy_events[0]["payload"]["curated"] is True
    assert policy_events[0]["payload"]["retained_only"] is False


def test_scope_action_cannot_be_resolved_twice_with_conflicting_semantics(
    promotion_config,
) -> None:
    _runs, _parent, actions, action = _seed_scope_action(promotion_config)
    actions.fork(
        action.action_id,
        "issue313 first revised objective",
        reason="first human scope decision",
        authorized_by="issue313-operator",
    )
    with pytest.raises(OperatorActionConflictError, match="already resolved"):
        actions.fork(
            action.action_id,
            "issue313 conflicting revised objective",
            reason="conflicting second scope decision",
            authorized_by="issue313-operator",
        )
