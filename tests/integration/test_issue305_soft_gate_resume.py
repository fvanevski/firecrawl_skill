from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from asset_promotion_test_support import TEST_DSN, _seed_retained_assets

from firecrawl_skill.research_store.acquisition.candidate_ranking import CandidateBudget
from firecrawl_skill.research_store.asset_promotion_service import AssetPromotionService
from firecrawl_skill.research_store.candidate_budget_outcomes import (
    CandidateBudgetHardRejected,
    CandidateBudgetOverrideRequired,
)
from firecrawl_skill.research_store.candidate_policy_service import CandidatePolicyError
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.smart_result import (
    SMART_RESUMABLE_EXIT,
    OperatorActionOrchestratorResult,
    smart_cli_disposition,
)

pytest_plugins = ("asset_promotion_test_support",)
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


def _completion_check(service: AssetPromotionService, run_id: UUID) -> dict:
    checks = [
        item
        for item in service.candidate_policy_service.list_checks(run_id)
        if item["phase"] == "completion_admission"
    ]
    assert len(checks) == 1
    return checks[0]


def test_exact_soft_override_resumes_same_run_and_seals_membership(
    promotion_config: StoreConfig,
) -> None:
    _corpus, runs, status, _manifest = _seed_retained_assets(promotion_config)
    run_id = status.id
    revision = status.lifecycle_revision
    budget = replace(CandidateBudget(), max_per_asset_contribution_chunks=0)
    service = AssetPromotionService(runs.uow_factory, candidate_budget=budget)

    with pytest.raises(CandidateBudgetOverrideRequired) as exc_info:
        service.prepare_for_indexing(run_id, lifecycle_revision=revision)

    assert runs.status(run_id=run_id).state == "indexing"
    assert runs.status(run_id=run_id).lifecycle_revision == revision
    assert service.get_active_seal(run_id) is None
    check = _completion_check(service, run_id)
    boundary = exc_info.value
    assert boundary.context.run_id == run_id
    assert boundary.context.lifecycle_revision == revision
    assert boundary.context.check_id == UUID(check["id"])
    assert boundary.context.scope_fingerprint == check["content_sha256"]
    assert boundary.context.violated_limits == ("max_per_asset_contribution_chunks",)
    cli_result = OperatorActionOrchestratorResult(
        run_id=run_id,
        final_state="indexing",
        outcome="operator_action_required",
        operator_action=boundary.to_dict(),
    )
    assert smart_cli_disposition(cli_result).exit_code == SMART_RESUMABLE_EXIT

    override_id = service.candidate_policy_service.record_override(
        run_id,
        boundary.context.check_id,
        "max_per_asset_contribution_chunks",
        reason="issue 305 exact-scope recovery regression",
        author="integration-test",
    )
    assert isinstance(override_id, UUID)

    seal = service.prepare_for_indexing(run_id, lifecycle_revision=revision)

    assert seal.run_id == run_id
    assert seal.lifecycle_revision == revision
    assert seal.status == "sealed"
    assert seal.expected_asset_count == 1
    assert seal.expected_chunk_count == len(seal.chunk_ids)
    assert service.get_active_seal(run_id) == seal
    assert runs.status(run_id=run_id).state == "indexing"
    check_after = _completion_check(service, run_id)
    assert check_after["id"] == check["id"]
    assert check_after["content_sha256"] == check["content_sha256"]
    assert check_after["overridden_limits"] == ["max_per_asset_contribution_chunks"]


def test_hard_completion_violation_cannot_be_overridden_or_sealed(
    promotion_config: StoreConfig,
) -> None:
    _corpus, runs, status, _manifest = _seed_retained_assets(promotion_config)
    run_id = status.id
    revision = status.lifecycle_revision
    budget = replace(CandidateBudget(), max_chunks=0)
    service = AssetPromotionService(runs.uow_factory, candidate_budget=budget)

    with pytest.raises(CandidateBudgetHardRejected) as exc_info:
        service.prepare_for_indexing(run_id, lifecycle_revision=revision)

    assert service.get_active_seal(run_id) is None
    check = _completion_check(service, run_id)
    assert exc_info.value.context.check_id == UUID(check["id"])
    assert [item["limit_name"] for item in check["hard_violations"]] == ["max_chunks"]
    assert check["soft_violations"] == []

    with pytest.raises(
        CandidatePolicyError, match="hard limit .* cannot be overridden"
    ):
        service.candidate_policy_service.record_override(
            run_id,
            UUID(check["id"]),
            "max_chunks",
            reason="must remain forbidden",
            author="integration-test",
        )

    assert service.get_active_seal(run_id) is None
    assert runs.status(run_id=run_id).state == "indexing"
    assert runs.status(run_id=run_id).lifecycle_revision == revision
