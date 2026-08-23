"""Issue #300 AC5 exact recency and temporal qualification regressions."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from firecrawl_skill.research_store.recency import (
    RecencyParseError,
    normalize_recency_window,
    parse_recency_window,
)
from firecrawl_skill.research_store.temporal_policy import (
    freshness_satisfied,
    passage_temporally_qualifies,
    publication_in_window,
)


def test_qdr_5d_keeps_exact_local_window_and_uses_provider_superset() -> None:
    window = normalize_recency_window("qdr:5d")
    assert window is not None
    assert window.exact_days == 5
    assert window.exact_seconds == 5 * 24 * 60 * 60
    assert window.provider_tbs == "qdr:w"
    assert window.to_dict()["authority"] == "local_exact_window"


@pytest.mark.parametrize(
    ("value", "provider"),
    [
        ("qdr:h", "qdr:h"),
        ("qdr:24h", "qdr:d"),
        ("qdr:2d", "qdr:w"),
        ("qdr:7d", "qdr:w"),
        ("qdr:8d", "qdr:m"),
        ("qdr:30d", "qdr:m"),
        ("qdr:32d", "qdr:y"),
        ("qdr:367d", None),
    ],
)
def test_provider_filter_is_never_narrower_than_exact_request(
    value: str, provider: str | None
) -> None:
    window = normalize_recency_window(value)
    assert window is not None
    assert window.provider_tbs == provider


def test_unsupported_explicit_recency_never_falls_back() -> None:
    with pytest.raises(RecencyParseError):
        parse_recency_window("qdr:5x")


def test_date_only_end_includes_the_complete_named_day() -> None:
    window = {"start": "2026-08-17", "end": "2026-08-22"}
    assert publication_in_window("2026-08-22T23:59:59+00:00", window)
    assert not publication_in_window("2026-08-23T00:00:00+00:00", window)


def test_missing_publication_never_satisfies_publication_window() -> None:
    assert not publication_in_window(
        None,
        {"start": "2026-08-17", "end": "2026-08-22"},
    )


def test_retrieval_only_does_not_satisfy_temporal_spec() -> None:
    passage = {
        "published_at": None,
        "updated_at": None,
        "retrieved_at": "2026-08-22T12:00:00+00:00",
    }
    spec = {
        "time_window": {"start": "2026-08-17", "end": "2026-08-22"},
        "freshness_requirements": [{"max_age_days": 5}],
    }
    assert not passage_temporally_qualifies(
        passage,
        spec,
        now=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
    )


def test_old_publication_recent_update_can_satisfy_max_age_not_publication_window() -> (
    None
):
    now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    assert freshness_satisfied(
        published_at="2020-01-01T00:00:00+00:00",
        updated_at="2026-08-21T12:00:00+00:00",
        max_age_days=5,
        now=now,
    )
    assert not passage_temporally_qualifies(
        {
            "published_at": "2020-01-01T00:00:00+00:00",
            "updated_at": "2026-08-21T12:00:00+00:00",
        },
        {
            "time_window": {"start": "2026-08-17", "end": "2026-08-22"},
            "freshness_requirements": [{"max_age_days": 5}],
        },
        now=now,
    )


def test_five_day_max_age_boundary_is_inclusive() -> None:
    now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    assert freshness_satisfied(
        published_at="2026-08-17T12:00:00+00:00",
        updated_at=None,
        max_age_days=5,
        now=now,
    )
    assert not freshness_satisfied(
        published_at="2026-08-17T11:59:59+00:00",
        updated_at=None,
        max_age_days=5,
        now=now,
    )


def test_explicit_recency_penalizes_undated_candidate_in_ranking(monkeypatch) -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    from firecrawl_skill.research_domain.models import FreshnessStatus
    from firecrawl_skill.research_store.acquisition.candidate_ranking import (
        RankingPolicy,
        RankingScore,
        UrlType,
    )
    from firecrawl_skill.research_store.fsearch_policy_service import (
        PolicyFSearchService,
        _RankedCandidate,
    )
    from firecrawl_skill.research_store.temporal_fsearch_policy import (
        TemporalPolicyFSearchService,
    )

    candidate_id = uuid4()
    candidate = _RankedCandidate(
        candidate={},
        candidate_id=candidate_id,
        source_rank=1,
        url="https://example.test/undated",
        url_type=UrlType.ARTICLE,
        freshness_status=FreshnessStatus.NOT_APPLICABLE,
        freshness_rationale="no published date available",
        stale_after_days=5,
        is_duplicate=False,
        expected_char_count=5000,
        score=RankingScore(1.0, 0.0, 0.0, 0.0, 0.0, 1.0, "baseline"),
    )
    monkeypatch.setattr(
        PolicyFSearchService,
        "_rank_candidates",
        lambda *_args, **_kwargs: [candidate],
    )
    service = object.__new__(TemporalPolicyFSearchService)
    service.ranking_policy = RankingPolicy()
    service.run_service = SimpleNamespace(
        get_candidate=lambda *_args, **_kwargs: {"published_at": None}
    )
    token = service._recency_window.set(normalize_recency_window("qdr:5d"))
    try:
        result = service._rank_candidates(
            uuid4(), [SimpleNamespace()], stale_after_days=5
        )
    finally:
        service._recency_window.reset(token)

    assert result[0].freshness_status is FreshnessStatus.UNSATISFIED
    assert (
        result[0].score.freshness_penalty == service.ranking_policy.stale_date_penalty
    )


def test_hour_recency_uses_exact_seconds_not_rounded_day(monkeypatch) -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    from firecrawl_skill.research_domain.models import FreshnessStatus
    from firecrawl_skill.research_store.acquisition.candidate_ranking import (
        RankingPolicy,
        RankingScore,
        UrlType,
    )
    from firecrawl_skill.research_store.fsearch_policy_service import (
        PolicyFSearchService,
        _RankedCandidate,
    )
    from firecrawl_skill.research_store.temporal_fsearch_policy import (
        TemporalPolicyFSearchService,
    )

    now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    candidate_id = uuid4()
    candidate = _RankedCandidate(
        candidate={},
        candidate_id=candidate_id,
        source_rank=1,
        url="https://example.test/six-hours-old",
        url_type=UrlType.ARTICLE,
        freshness_status=FreshnessStatus.SATISFIED,
        freshness_rationale="legacy rounded-day result",
        stale_after_days=1,
        is_duplicate=False,
        expected_char_count=5000,
        score=RankingScore(1.0, 0.0, 0.0, 0.0, 0.0, 1.0, "baseline"),
    )
    monkeypatch.setattr(
        PolicyFSearchService,
        "_rank_candidates",
        lambda *_args, **_kwargs: [candidate],
    )
    monkeypatch.setattr(
        "firecrawl_skill.research_store.temporal_fsearch_policy.utcnow", lambda: now
    )
    service = object.__new__(TemporalPolicyFSearchService)
    service.ranking_policy = RankingPolicy()
    service.run_service = SimpleNamespace(
        get_candidate=lambda *_args, **_kwargs: {
            "published_at": datetime(2026, 8, 22, 6, tzinfo=timezone.utc)
        }
    )
    window = normalize_recency_window("qdr:5h")
    assert window is not None and window.exact_seconds == 5 * 60 * 60
    token = service._recency_window.set(window)
    try:
        result = service._rank_candidates(
            uuid4(), [SimpleNamespace()], stale_after_days=1
        )
    finally:
        service._recency_window.reset(token)

    assert result[0].freshness_status is FreshnessStatus.UNSATISFIED
    assert "exceeds exact qdr:5h" in result[0].freshness_rationale


def test_publication_window_rejects_undated_and_out_of_window() -> None:
    from firecrawl_skill.research_store.temporal_policy import publication_in_window

    window = {"start": "2026-08-10", "end": "2026-08-14"}
    assert publication_in_window("2026-08-10T00:00:00Z", window)
    assert publication_in_window("2026-08-14T23:59:59Z", window)
    assert not publication_in_window("2026-08-15T00:00:00Z", window)
    assert not publication_in_window("2026-08-09T23:59:59Z", window)
    assert not publication_in_window(None, window)
