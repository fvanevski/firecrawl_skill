"""Completion-membership budget gates for issue #215."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import asset_promotion_test_support as _promotion_support
from asset_promotion_test_support import TEST_DSN, _seed_retained_assets

from firecrawl_skill.research_store.acquisition.candidate_ranking import CandidateBudget
from firecrawl_skill.research_store.asset_promotion_models import AssetPromotionError
from firecrawl_skill.research_store.asset_promotion_service import AssetPromotionService
from firecrawl_skill.research_store.candidate_policy_service import CandidatePolicyError

promotion_config = _promotion_support.promotion_config
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


def test_completion_admission_requires_explicit_soft_override(promotion_config):
    _corpus, runs, status, _manifest = _seed_retained_assets(promotion_config, count=1)
    service = AssetPromotionService(runs.uow_factory)
    service.candidate_budget = CandidateBudget(
        max_per_asset_contribution_chunks=0,
        max_generic_page_share=1.0,
    )

    with pytest.raises(AssetPromotionError, match="override required"):
        service.prepare_for_indexing(
            status.id,
            lifecycle_revision=status.lifecycle_revision,
            actor_type="integration-test",
        )

    checks = service.candidate_policy_service.list_checks(status.id)
    completion = [item for item in checks if item["phase"] == "completion_admission"]
    assert len(completion) == 1
    check = completion[0]
    assert check["overridden_limits"] == []
    assert any(
        item["limit_name"] == "max_per_asset_contribution_chunks"
        for item in check["soft_violations"]
    )

    service.candidate_policy_service.record_override(
        status.id,
        check_id=UUID(check["id"]),
        limit_name="max_per_asset_contribution_chunks",
        reason="Explicitly retain this single authoritative asset for the narrow run.",
        author="integration-test",
    )
    seal = service.prepare_for_indexing(
        status.id,
        lifecycle_revision=status.lifecycle_revision,
        actor_type="integration-test",
    )
    assert seal.status == "sealed"
    assert seal.expected_asset_count == 1


def test_completion_hard_limit_rejects_and_cannot_be_overridden(promotion_config):
    _corpus, runs, status, _manifest = _seed_retained_assets(promotion_config, count=1)
    service = AssetPromotionService(runs.uow_factory)
    service.candidate_budget = CandidateBudget(
        max_chunks=0,
        max_generic_page_share=1.0,
    )

    with pytest.raises(AssetPromotionError, match="hard limit"):
        service.prepare_for_indexing(
            status.id,
            lifecycle_revision=status.lifecycle_revision,
            actor_type="integration-test",
        )
    check = next(
        item
        for item in service.candidate_policy_service.list_checks(status.id)
        if item["phase"] == "completion_admission"
    )
    with pytest.raises(
        CandidatePolicyError,
        match="hard candidate-budget violations cannot be overridden",
    ):
        service.candidate_policy_service.record_override(
            status.id,
            check_id=UUID(check["id"]),
            limit_name="max_chunks",
            reason="hard limit must not be bypassed",
            author="integration-test",
        )
    assert service.get_active_seal(status.id) is None


def test_direct_completion_promotion_cannot_bypass_missing_budget_check(
    promotion_config,
):
    _corpus, runs, status, _manifest = _seed_retained_assets(promotion_config, count=1)
    service = AssetPromotionService(runs.uow_factory)
    asset = service.list_assets(status.id)[0]
    service.promote(
        UUID(asset["id"]),
        "evidence_eligible",
        expected_lifecycle_revision=status.lifecycle_revision,
        expected_run_id=status.id,
        actor_type="integration-test",
        actor_identifier="issue-215",
        policy_version="candidate-budget-v1",
        reason_code="test_evidence",
        reason="prepare bypass test",
    )
    with pytest.raises(AssetPromotionError, match="budget check"):
        service.promote(
            UUID(asset["id"]),
            "completion_critical",
            expected_lifecycle_revision=status.lifecycle_revision,
            expected_run_id=status.id,
            actor_type="integration-test",
            actor_identifier="issue-215",
            policy_version="candidate-budget-v1",
            reason_code="test_bypass",
            reason="must fail",
        )
