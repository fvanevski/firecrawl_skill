from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from asset_promotion_test_support import TEST_DSN, _seed_retained_assets

from firecrawl_skill.research_store.acquisition.candidate_ranking import CandidateBudget
from firecrawl_skill.research_store.asset_promotion_models import AssetPromotionError
from firecrawl_skill.research_store.asset_promotion_service import AssetPromotionService
from firecrawl_skill.research_store.candidate_policy_service import CandidatePolicyError
from firecrawl_skill.research_store.config import StoreConfig

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

    with pytest.raises(AssetPromotionError, match="override required"):
        service.prepare_for_indexing(run_id, lifecycle_revision=revision)

    assert runs.status(run_id=run_id).state == "indexing"
    assert runs.status(run_id=run_id).lifecycle_revision == revision
    assert service.get_active_seal(run_id) is None
    check = _completion_check(service, run_id)
    assert check["lifecycle_revision"] == revision
    assert check["hard_violations"] == []
    assert [item["limit_name"] for item in check["soft_violations"]] == [
        "max_per_asset_contribution_chunks"
    ]
    assert check["overridden_limits"] == []

    override_id = service.candidate_policy_service.record_override(
        run_id,
        UUID(check["id"]),
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
    assert check_after["overridden_limits"] == [
        "max_per_asset_contribution_chunks"
    ]


def test_hard_completion_violation_cannot_be_overridden_or_sealed(
    promotion_config: StoreConfig,
) -> None:
    _corpus, runs, status, _manifest = _seed_retained_assets(promotion_config)
    run_id = status.id
    revision = status.lifecycle_revision
    budget = replace(CandidateBudget(), max_chunks=0)
    service = AssetPromotionService(runs.uow_factory, candidate_budget=budget)

    with pytest.raises(AssetPromotionError, match="hard limit rejected"):
        service.prepare_for_indexing(run_id, lifecycle_revision=revision)

    assert service.get_active_seal(run_id) is None
    check = _completion_check(service, run_id)
    assert [item["limit_name"] for item in check["hard_violations"]] == ["max_chunks"]
    assert check["soft_violations"] == []

    with pytest.raises(CandidatePolicyError, match="hard limit .* cannot be overridden"):
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
