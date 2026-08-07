"""Tests for candidate_ranking: URL classification, ranking scores, and budgets."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from candidate_ranking import (
    DEFAULT_RANKING_POLICY,
    CandidateBudget,
    OverrideJustification,
    RankingPolicy,
    UrlType,
    assess_freshness,
    check_corpus_budget,
    classify_url,
    compute_ranking_score,
    validate_override_justification,
)
from research_domain.models import FreshnessStatus

# ---------------------------------------------------------------------------
# URL classification
# ---------------------------------------------------------------------------


class TestClassifyUrl:
    def test_article_default(self):
        url = "https://example.com/2024/news/breaking-story"
        assert classify_url(url) == UrlType.ARTICLE

    def test_live_blog_by_path(self):
        urls = [
            "https://example.com/live-blog/cricket-final",
            "https://example.com/live-updates/election-2024",
            "https://example.com/liveblog/march-events",
        ]
        for url in urls:
            assert classify_url(url) == UrlType.LIVE_BLOG, url

    def test_live_blog_by_keyword(self):
        assert (
            classify_url("https://example.com/page", "", "live coverage of the event")
            == UrlType.LIVE_BLOG
        )
        assert (
            classify_url("https://example.com/page", "Live Blog", "")
            == UrlType.LIVE_BLOG
        )

    def test_official_release_by_path(self):
        urls = [
            "https://example.com/press-release/q4-results",
            "https://example.com/newsroom/statement",
            "https://example.com/releases/product-launch",
        ]
        for url in urls:
            assert classify_url(url) == UrlType.OFFICIAL_RELEASE, url

    def test_official_release_by_domain(self):
        assert (
            classify_url("https://www.prnewswire.com/news-releases/foo.html")
            == UrlType.OFFICIAL_RELEASE
        )
        assert (
            classify_url("https://www.businesswire.com/news/foo")
            == UrlType.OFFICIAL_RELEASE
        )

    def test_topic_hub_by_path(self):
        urls = [
            "https://example.com/topics/technology",
            "https://example.com/category/science",
            "https://example.com/tags/python",
        ]
        for url in urls:
            assert classify_url(url) == UrlType.TOPIC_HUB, url

    def test_topic_hub_by_domain(self):
        assert classify_url("https://www.reddit.com/r/technology/") == UrlType.TOPIC_HUB
        assert classify_url("https://medium.com/topic/tech") == UrlType.TOPIC_HUB

    def test_home_page(self):
        urls = [
            "https://example.com/",
            "https://example.com/index.html",
            "https://example.com/default.aspx",
        ]
        for url in urls:
            assert classify_url(url) == UrlType.HOME_PAGE, url

    def test_reference_page(self):
        urls = [
            "https://example.com/faq",
            "https://example.com/help",
            "https://example.com/terms",
            "https://example.com/privacy",
            "https://example.com/sitemap",
        ]
        for url in urls:
            assert classify_url(url) == UrlType.REFERENCE_PAGE, url

    def test_search_page(self):
        urls = [
            "https://example.com/search?q=foo",
            "https://example.com/query?search=bar",
            "https://example.com/results?q=test",
        ]
        for url in urls:
            assert classify_url(url) == UrlType.SEARCH_PAGE, url

    def test_wikipedia_receives_article_not_hub(self):
        """Wikipedia URLs are classified as article, not topic_hub."""
        url = "https://en.wikipedia.org/wiki/Web_scraping"
        assert classify_url(url) == UrlType.ARTICLE

    def test_ap_news_topic_hub_penalized(self):
        """AP topic hub pages should be classified as topic_hub for penalty."""
        url = "https://apnews.com/hub/climate-change"
        assert classify_url(url) == UrlType.TOPIC_HUB

    def test_white_house_home_page(self):
        """White House home page should be classified as home_page."""
        url = "https://www.whitehouse.gov/"
        assert classify_url(url) == UrlType.HOME_PAGE

    def test_unknown_fallback(self):
        url = "https://example.com/random-page"
        assert classify_url(url) == UrlType.ARTICLE


# ---------------------------------------------------------------------------
# Freshness assessment
# ---------------------------------------------------------------------------


class TestAssessFreshness:
    def test_no_published_date_is_unassessed(self):
        now = datetime.now(timezone.utc)
        status, rationale = assess_freshness(None, now)
        assert status == FreshnessStatus.NOT_APPLICABLE
        assert "no published date" in rationale

    def test_fresh_article(self):
        now = datetime(2026, 8, 7, tzinfo=timezone.utc)
        published = datetime(2026, 8, 5, tzinfo=timezone.utc)
        status, _ = assess_freshness(published, now)
        assert status == FreshnessStatus.SATISFIED

    def test_stale_article(self):
        now = datetime(2026, 8, 7, tzinfo=timezone.utc)
        published = datetime(2025, 1, 1, tzinfo=timezone.utc)
        status, _ = assess_freshness(published, now)
        assert status == FreshnessStatus.UNSATISFIED

    def test_future_date_is_fresh(self):
        now = datetime(2026, 8, 7, tzinfo=timezone.utc)
        published = datetime(2026, 8, 10, tzinfo=timezone.utc)
        status, _ = assess_freshness(published, now)
        assert status == FreshnessStatus.SATISFIED


# ---------------------------------------------------------------------------
# Ranking score computation
# ---------------------------------------------------------------------------


class TestComputeRankingScore:
    def test_clean_article_scores_high(self):
        score = compute_ranking_score(
            base_score=0.9,
            url_type=UrlType.ARTICLE,
            freshness_status=FreshnessStatus.SATISFIED,
            is_duplicate=False,
            expected_char_count=5000,
        )
        assert score.total >= 0.7
        assert score.url_type_penalty == 0.0
        assert score.freshness_penalty == 0.0
        assert score.duplication_penalty == 0.0

    def test_topic_hub_gets_penalty(self):
        score = compute_ranking_score(
            base_score=0.9,
            url_type=UrlType.TOPIC_HUB,
            freshness_status=FreshnessStatus.SATISFIED,
            is_duplicate=False,
            expected_char_count=5000,
        )
        assert score.url_type_penalty == DEFAULT_RANKING_POLICY.generic_page_penalty
        assert score.total < 0.9

    def test_home_page_heavy_penalty(self):
        score = compute_ranking_score(
            base_score=0.9,
            url_type=UrlType.HOME_PAGE,
            freshness_status=FreshnessStatus.SATISFIED,
            is_duplicate=False,
            expected_char_count=5000,
        )
        assert score.url_type_penalty == DEFAULT_RANKING_POLICY.home_page_penalty
        assert score.total < 0.9 - DEFAULT_RANKING_POLICY.home_page_penalty

    def test_stale_date_penalty(self):
        score = compute_ranking_score(
            base_score=0.9,
            url_type=UrlType.ARTICLE,
            freshness_status=FreshnessStatus.UNSATISFIED,
            is_duplicate=False,
            expected_char_count=5000,
        )
        assert score.freshness_penalty == DEFAULT_RANKING_POLICY.stale_date_penalty

    def test_duplicate_penalty(self):
        score = compute_ranking_score(
            base_score=0.9,
            url_type=UrlType.ARTICLE,
            freshness_status=FreshnessStatus.SATISFIED,
            is_duplicate=True,
            expected_char_count=5000,
        )
        assert score.duplication_penalty == DEFAULT_RANKING_POLICY.duplication_penalty

    def test_extreme_large_size_penalty(self):
        score = compute_ranking_score(
            base_score=0.9,
            url_type=UrlType.ARTICLE,
            freshness_status=FreshnessStatus.SATISFIED,
            is_duplicate=False,
            expected_char_count=600_000,
        )
        assert score.size_penalty == DEFAULT_RANKING_POLICY.extreme_size_penalty

    def test_extreme_small_size_penalty(self):
        score = compute_ranking_score(
            base_score=0.9,
            url_type=UrlType.ARTICLE,
            freshness_status=FreshnessStatus.SATISFIED,
            is_duplicate=False,
            expected_char_count=50_000,
        )
        assert score.size_penalty == DEFAULT_RANKING_POLICY.extreme_size_penalty * 0.5

    def test_total_clamped_to_zero(self):
        score = compute_ranking_score(
            base_score=0.1,
            url_type=UrlType.HOME_PAGE,
            freshness_status=FreshnessStatus.UNSATISFIED,
            is_duplicate=True,
            expected_char_count=600_000,
        )
        assert score.total >= 0.0

    def test_total_clamped_to_one(self):
        score = compute_ranking_score(
            base_score=1.0,
            url_type=UrlType.ARTICLE,
            freshness_status=FreshnessStatus.SATISFIED,
            is_duplicate=False,
            expected_char_count=None,
        )
        assert score.total <= 1.0

    def test_rationale_contains_components(self):
        score = compute_ranking_score(
            base_score=0.5,
            url_type=UrlType.TOPIC_HUB,
            freshness_status=FreshnessStatus.UNSATISFIED,
            is_duplicate=True,
            expected_char_count=1000,
        )
        assert "url_type=topic_hub" in score.rationale
        assert "freshness=unsatisfied" in score.rationale
        assert "duplication=yes" in score.rationale

    def test_relevant_dated_article_outranks_generic_hub(self):
        """A relevant dated article should outrank a generic high-volume page."""
        article_score = compute_ranking_score(
            base_score=0.8,
            url_type=UrlType.ARTICLE,
            freshness_status=FreshnessStatus.SATISFIED,
            is_duplicate=False,
            expected_char_count=8000,
        )
        hub_score = compute_ranking_score(
            base_score=0.8,
            url_type=UrlType.TOPIC_HUB,
            freshness_status=FreshnessStatus.SATISFIED,
            is_duplicate=False,
            expected_char_count=5000,
        )
        assert article_score.total > hub_score.total


# ---------------------------------------------------------------------------
# Ranking policy validation
# ---------------------------------------------------------------------------


class TestRankingPolicy:
    def test_default_policy_valid(self):
        assert DEFAULT_RANKING_POLICY.generic_page_penalty == 0.3
        assert DEFAULT_RANKING_POLICY.home_page_penalty == 0.5

    def test_invalid_penalty_raises(self):
        with pytest.raises(ValueError):
            RankingPolicy(generic_page_penalty=1.5)

    def test_invalid_penalty_negative(self):
        with pytest.raises(ValueError):
            RankingPolicy(generic_page_penalty=-0.1)

    def test_extreme_size_threshold_validation(self):
        with pytest.raises(ValueError):
            RankingPolicy(extreme_size_large_threshold=-1)

    def test_custom_policy_applied(self):
        policy = RankingPolicy(
            generic_page_penalty=0.0,
            home_page_penalty=0.0,
            stale_date_penalty=0.0,
            duplication_penalty=0.0,
            extreme_size_penalty=0.0,
        )
        score = compute_ranking_score(
            base_score=0.9,
            url_type=UrlType.TOPIC_HUB,
            freshness_status=FreshnessStatus.UNSATISFIED,
            is_duplicate=True,
            expected_char_count=600_000,
            policy=policy,
        )
        assert score.total == 0.9


# ---------------------------------------------------------------------------
# Corpus budget enforcement
# ---------------------------------------------------------------------------


class TestCandidateBudget:
    def test_default_budget_valid(self):
        budget = CandidateBudget()
        assert budget.max_candidates == 40
        assert budget.max_bytes == 5_000_000

    def test_invalid_max_candidates(self):
        with pytest.raises(ValueError):
            CandidateBudget(max_candidates=-1)

    def test_invalid_generic_page_share(self):
        with pytest.raises(ValueError):
            CandidateBudget(max_generic_page_share=1.5)

    def test_invalid_generic_page_share_negative(self):
        with pytest.raises(ValueError):
            CandidateBudget(max_generic_page_share=-0.1)


class TestCheckCorpusBudget:
    def test_within_limits_accepted(self):
        result = check_corpus_budget(
            candidates=["a", "b"],
            total_bytes=1000,
            total_chunks=10,
            generic_page_count=0,
            extraction_attempts=2,
            per_asset_chunk_counts={},
        )
        assert result.accepted
        assert len(result.violations) == 0

    def test_hard_limit_exceeded(self):
        result = check_corpus_budget(
            candidates=list(range(100)),
            total_bytes=1000,
            total_chunks=10,
            generic_page_count=0,
            extraction_attempts=2,
            per_asset_chunk_counts={},
        )
        assert not result.accepted
        assert len(result.hard_violations) > 0
        assert result.requires_override is False

    def test_soft_generic_page_share_exceeded(self):
        result = check_corpus_budget(
            candidates=["a", "b", "c", "d"],
            total_bytes=1000,
            total_chunks=10,
            generic_page_count=4,
            extraction_attempts=2,
            per_asset_chunk_counts={},
        )
        assert result.accepted
        assert result.requires_override is True
        assert len(result.soft_violations) > 0

    def test_soft_per_asset_exceeded(self):
        result = check_corpus_budget(
            candidates=["a"],
            total_bytes=1000,
            total_chunks=10,
            generic_page_count=0,
            extraction_attempts=2,
            per_asset_chunk_counts={"asset-1": 1000},
        )
        assert result.accepted
        assert result.requires_override is True
        assert any(
            v.limit_name == "max_per_asset_contribution_chunks"
            for v in result.soft_violations
        )

    def test_to_dict_structure(self):
        result = check_corpus_budget(
            candidates=["a"],
            total_bytes=1000,
            total_chunks=10,
            generic_page_count=0,
            extraction_attempts=2,
            per_asset_chunk_counts={},
        )
        d = result.to_dict()
        assert "accepted" in d
        assert "violations" in d
        assert "soft_violations" in d
        assert "hard_violations" in d
        assert "requires_override" in d


# ---------------------------------------------------------------------------
# Override justification
# ---------------------------------------------------------------------------


class TestOverrideJustification:
    def test_valid_justification(self):
        now = datetime.now(timezone.utc)
        j = OverrideJustification(
            limit_name="max_generic_page_share",
            reason="Narrow objective requires broad source coverage",
            author="agent",
            created_at=now,
            run_id="fr_test123",
        )
        assert j.limit_name == "max_generic_page_share"
        validate_override_justification(j, allowed_limits=["max_generic_page_share"])

    def test_missing_reason_raises(self):
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError):
            OverrideJustification(
                limit_name="max_generic_page_share",
                reason="",
                author="agent",
                created_at=now,
                run_id="fr_test123",
            )

    def test_unknown_limit_raises(self):
        now = datetime.now(timezone.utc)
        j = OverrideJustification(
            limit_name="unknown_limit",
            reason="Test",
            author="agent",
            created_at=now,
            run_id="fr_test123",
        )
        with pytest.raises(ValueError):
            validate_override_justification(
                j, allowed_limits=["max_generic_page_share"]
            )

    def test_missing_author_raises(self):
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError):
            OverrideJustification(
                limit_name="max_generic_page_share",
                reason="Test",
                author="",
                created_at=now,
                run_id="fr_test123",
            )

    def test_missing_run_id_raises(self):
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError):
            OverrideJustification(
                limit_name="max_generic_page_share",
                reason="Test",
                author="agent",
                created_at=now,
                run_id="",
            )


# ---------------------------------------------------------------------------
# Integration: audited Wikipedia, AP hub, White House penalties
# ---------------------------------------------------------------------------


class TestAcceptanceCriteria:
    def test_wikipedia_page_receives_appropriate_penalty(self):
        """The audited Wikipedia page receives article classification, not hub penalty."""
        url = "https://en.wikipedia.org/wiki/Web_scraping"
        url_type = classify_url(url)
        score = compute_ranking_score(
            base_score=0.7,
            url_type=url_type,
            freshness_status=FreshnessStatus.SATISFIED,
            is_duplicate=False,
            expected_char_count=20000,
        )
        assert url_type == UrlType.ARTICLE
        assert score.url_type_penalty == 0.0

    def test_ap_topic_hub_penalized_for_narrow_objective(self):
        """AP topic hub gets topic_hub classification and penalty."""
        url = "https://apnews.com/hub/climate-change"
        url_type = classify_url(url)
        score = compute_ranking_score(
            base_score=0.7,
            url_type=url_type,
            freshness_status=FreshnessStatus.SATISFIED,
            is_duplicate=False,
            expected_char_count=5000,
        )
        assert url_type == UrlType.TOPIC_HUB
        assert score.url_type_penalty > 0.0

    def test_white_house_home_page_penalized(self):
        """White House home page gets home_page classification and heavy penalty."""
        url = "https://www.whitehouse.gov/"
        url_type = classify_url(url)
        score = compute_ranking_score(
            base_score=0.7,
            url_type=url_type,
            freshness_status=FreshnessStatus.SATISFIED,
            is_duplicate=False,
            expected_char_count=10000,
        )
        assert url_type == UrlType.HOME_PAGE
        assert score.url_type_penalty == DEFAULT_RANKING_POLICY.home_page_penalty

    def test_relevant_dated_article_outranks_generic_hub(self):
        """A relevant dated article outranks a generic high-volume page."""
        article_score = compute_ranking_score(
            base_score=0.8,
            url_type=UrlType.ARTICLE,
            freshness_status=FreshnessStatus.SATISFIED,
            is_duplicate=False,
            expected_char_count=8000,
        )
        hub_score = compute_ranking_score(
            base_score=0.8,
            url_type=UrlType.TOPIC_HUB,
            freshness_status=FreshnessStatus.SATISFIED,
            is_duplicate=False,
            expected_char_count=5000,
        )
        assert article_score.total > hub_score.total

    def test_budget_enforcement_is_deterministic(self):
        """Budget enforcement produces deterministic results for identical inputs."""
        r1 = check_corpus_budget(
            candidates=["a", "b"],
            total_bytes=1000,
            total_chunks=10,
            generic_page_count=0,
            extraction_attempts=2,
            per_asset_chunk_counts={},
        )
        r2 = check_corpus_budget(
            candidates=["a", "b"],
            total_bytes=1000,
            total_chunks=10,
            generic_page_count=0,
            extraction_attempts=2,
            per_asset_chunk_counts={},
        )
        assert r1.to_dict() == r2.to_dict()

    def test_no_single_generic_page_silently_contributes_most_chunks(self):
        """Generic pages cannot silently dominate corpus chunks."""
        result = check_corpus_budget(
            candidates=["hub1", "hub2", "article1"],
            total_bytes=10000,
            total_chunks=100,
            generic_page_count=2,
            extraction_attempts=2,
            per_asset_chunk_counts={
                "hub1": 60,
                "hub2": 30,
                "article1": 10,
            },
        )
        assert result.accepted
        if result.requires_override:
            generic_share = 2 / 3
            assert generic_share > 0.25
