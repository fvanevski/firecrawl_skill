"""Issue #215 unit coverage for URL ranking and corpus-budget rules."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from candidate_ranking import (
    CandidateBudget,
    OverrideJustification,
    RankingPolicy,
    UrlType,
    assess_freshness,
    check_corpus_budget,
    classify_url,
    compute_ranking_score,
    rank_to_base_score,
    validate_override_justification,
)
from classifier import classify_url_type
from research_domain.models import FreshnessStatus


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/2026/news/story", UrlType.ARTICLE),
        ("https://example.com/live-updates/election", UrlType.LIVE_BLOG),
        ("https://www.prnewswire.com/news-releases/x", UrlType.OFFICIAL_RELEASE),
        ("https://apnews.com/hub/climate-change", UrlType.TOPIC_HUB),
        ("https://www.whitehouse.gov/", UrlType.HOME_PAGE),
        ("https://en.wikipedia.org/wiki/Web_scraping", UrlType.REFERENCE_PAGE),
        ("https://example.com/search?q=research", UrlType.SEARCH_PAGE),
        ("not-a-url", UrlType.UNKNOWN),
        ("file:///tmp/local", UrlType.UNKNOWN),
    ],
)
def test_structural_url_types_are_reachable(url, expected):
    assert classify_url(url) == expected


def test_classifier_compatibility_surface_delegates_to_canonical_classifier():
    urls = [
        "https://apnews.com/hub/climate-change",
        "https://en.wikipedia.org/wiki/Web_scraping",
        "https://www.whitehouse.gov/",
        "not-a-url",
    ]
    for url in urls:
        assert classify_url_type(url) == classify_url(url).value


def test_wikipedia_reference_receives_narrow_objective_penalty():
    url_type = classify_url("https://en.wikipedia.org/wiki/Web_scraping")
    score = compute_ranking_score(
        0.8,
        url_type,
        FreshnessStatus.SATISFIED,
        False,
        20_000,
    )
    assert url_type == UrlType.REFERENCE_PAGE
    assert score.url_type_penalty > 0
    assert score.total < score.base_score


def test_ap_hub_and_white_house_home_receive_stronger_structural_penalties():
    ap = compute_ranking_score(
        0.8,
        classify_url("https://apnews.com/hub/climate-change"),
        FreshnessStatus.SATISFIED,
        False,
        20_000,
    )
    white_house = compute_ranking_score(
        0.8,
        classify_url("https://www.whitehouse.gov/"),
        FreshnessStatus.SATISFIED,
        False,
        20_000,
    )
    article = compute_ranking_score(
        0.8,
        UrlType.ARTICLE,
        FreshnessStatus.SATISFIED,
        False,
        20_000,
    )
    assert article.total > ap.total
    assert article.total > white_house.total


def test_freshness_threshold_matches_documented_contract():
    now = datetime(2026, 8, 7, tzinfo=timezone.utc)
    within = now - timedelta(days=365)
    outside = now - timedelta(days=366)
    within_status, within_reason = assess_freshness(within, now)
    outside_status, outside_reason = assess_freshness(outside, now)
    assert within_status == FreshnessStatus.SATISFIED
    assert outside_status == FreshnessStatus.UNSATISFIED
    assert "365" in within_reason
    assert "exceeds stale threshold 365" in outside_reason


def test_explicit_freshness_window_is_configurable():
    now = datetime(2026, 8, 7, tzinfo=timezone.utc)
    published = now - timedelta(days=8)
    status, _ = assess_freshness(published, now, stale_after_days=7)
    assert status == FreshnessStatus.UNSATISFIED


def test_provider_ordinal_translates_to_monotonic_base_score():
    values = [rank_to_base_score(rank, 5) for rank in range(1, 6)]
    assert values == sorted(values, reverse=True)
    assert values[0] == 1.0
    assert values[-1] == 0.0


def test_relevant_dated_article_outranks_generic_hub():
    article = compute_ranking_score(
        0.8,
        UrlType.ARTICLE,
        FreshnessStatus.SATISFIED,
        False,
        10_000,
    )
    hub = compute_ranking_score(
        0.8,
        UrlType.TOPIC_HUB,
        FreshnessStatus.SATISFIED,
        False,
        10_000,
    )
    assert article.total > hub.total


def test_ranking_penalizes_stale_duplicate_and_extreme_size():
    policy = RankingPolicy()
    score = compute_ranking_score(
        1.0,
        UrlType.ARTICLE,
        FreshnessStatus.UNSATISFIED,
        True,
        policy.extreme_size_large_threshold,
        policy=policy,
    )
    assert score.freshness_penalty == policy.stale_date_penalty
    assert score.duplication_penalty == policy.duplication_penalty
    assert score.size_penalty == policy.extreme_size_penalty
    assert "freshness=unsatisfied" in score.rationale
    assert "duplication=yes" in score.rationale


def test_normal_article_size_is_not_treated_as_extreme():
    score = compute_ranking_score(
        0.9,
        UrlType.ARTICLE,
        FreshnessStatus.SATISFIED,
        False,
        20_000,
    )
    assert score.size_penalty == 0.0


def test_budget_hard_limit_fails_closed_and_cannot_be_overridden():
    budget = CandidateBudget(max_candidates=1)
    result = check_corpus_budget(
        ["a", "b"],
        0,
        0,
        0,
        0,
        {},
        budget=budget,
    )
    assert not result.accepted
    assert not result.accepted_with_overrides(["max_candidates"])
    assert [item.limit_name for item in result.hard_violations] == ["max_candidates"]


def test_soft_generic_share_is_blocked_until_explicit_override():
    result = check_corpus_budget(
        ["hub", "article"],
        0,
        0,
        1,
        0,
        {},
        budget=CandidateBudget(max_generic_page_share=0.25),
    )
    assert not result.accepted
    assert result.requires_override
    assert not result.accepted_with_overrides([])
    assert result.accepted_with_overrides(["max_generic_page_share"])


def test_single_asset_cannot_silently_dominate_chunks():
    result = check_corpus_budget(
        ["asset-a", "asset-b"],
        1000,
        100,
        0,
        2,
        {"asset-a": 90, "asset-b": 10},
        budget=CandidateBudget(max_per_asset_contribution_chunks=50),
    )
    assert not result.accepted
    assert any(
        item.limit_name == "max_per_asset_contribution_chunks"
        for item in result.soft_violations
    )


def test_budget_is_deterministic_and_environment_configurable(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_BUDGET_MAX_CANDIDATES", "3")
    monkeypatch.setenv("FIRECRAWL_BUDGET_MAX_GENERIC_PAGE_SHARE", "0.5")
    budget = CandidateBudget.from_env()
    assert budget.max_candidates == 3
    assert budget.max_generic_page_share == 0.5
    args = {
        "candidates": ["a", "b"],
        "total_bytes": 100,
        "total_chunks": 2,
        "generic_page_count": 0,
        "extraction_attempts": 1,
        "per_asset_chunk_counts": {"a": 1, "b": 1},
        "budget": budget,
    }
    assert (
        check_corpus_budget(**args).to_dict() == check_corpus_budget(**args).to_dict()
    )


def test_ranking_policy_is_environment_configurable(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_RANK_REFERENCE_PAGE_PENALTY", "0.45")
    monkeypatch.setenv("FIRECRAWL_RANK_STALE_AFTER_DAYS", "14")
    policy = RankingPolicy.from_env()
    assert policy.reference_page_penalty == 0.45
    assert policy.stale_after_days == 14


def test_override_justification_validation_has_one_authoritative_check():
    justification = OverrideJustification(
        limit_name="max_generic_page_share",
        reason="The narrow objective requires this explicitly retained hub.",
        author="reviewer",
        created_at=datetime.now(timezone.utc),
        run_id="fr_test",
        budget_check_id="00000000-0000-0000-0000-000000000001",
    )
    validate_override_justification(
        justification,
        allowed_limits=["max_generic_page_share"],
    )
    with pytest.raises(ValueError, match="not in allowed limits"):
        validate_override_justification(
            justification,
            allowed_limits=["max_per_asset_contribution_chunks"],
        )
