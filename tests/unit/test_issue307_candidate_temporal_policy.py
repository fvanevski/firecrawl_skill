"""Issue #307 candidate temporal provenance/admission regressions."""

from __future__ import annotations

from datetime import datetime, timezone

from firecrawl_skill.research_store.candidate_temporal_policy import (
    assess_candidate_temporal,
)
from firecrawl_skill.research_store.temporal_candidate import (
    canonical_candidate_temporal,
    extract_document_temporal_signals,
)

CLOCK = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def _publication_spec():
    return {
        "time_window": {"start": "2026-08-18", "end": "2026-08-23"},
        "freshness_requirements": [],
    }


def _freshness_spec():
    return {
        "time_window": {"start": None, "end": None},
        "freshness_requirements": [{"max_age_days": 5}],
    }


def test_candidate_jsonld_distinguishes_published_and_modified() -> None:
    publication, signals = canonical_candidate_temporal(
        {
            "url": "https://example.test/article",
            "jsonLd": {
                "@type": "NewsArticle",
                "datePublished": "2026-01-01T00:00:00Z",
                "dateModified": "2026-08-22T00:00:00Z",
            },
        }
    )
    candidate = {
        "published_at": publication,
        "date_signals": signals,
    }

    pub = assess_candidate_temporal(candidate, _publication_spec(), now=CLOCK)
    fresh = assess_candidate_temporal(candidate, _freshness_spec(), now=CLOCK)
    assert pub.status == "ineligible"
    assert fresh.status == "eligible"
    assert fresh.updated_at is not None


def test_equivalent_offset_publication_signals_corroborate_same_instant() -> None:
    publication, signals = canonical_candidate_temporal(
        {
            "published_at": "2026-08-22T10:00:00Z",
            "metadata": {"datePublished": "2026-08-22T06:00:00-04:00"},
        }
    )

    assert publication == datetime(2026, 8, 22, 10, tzinfo=timezone.utc)
    assert signals["publication_status"] == "explicit_provider_valid"
    assert {item["raw"] for item in signals["publication_signals"]} == {
        "2026-08-22T10:00:00Z",
        "2026-08-22T06:00:00-04:00",
    }


def test_conflicting_explicit_publication_signals_fail_closed_unknown() -> None:
    publication, signals = canonical_candidate_temporal(
        {
            "publishedDate": "2026-08-20T00:00:00Z",
            "metadata": {"datePublished": "2026-08-21T00:00:00Z"},
        }
    )
    assert publication is None
    assert signals["publication_status"] == "explicit_provider_conflict"
    assessment = assess_candidate_temporal(
        {"published_at": publication, "date_signals": signals},
        _publication_spec(),
        now=CLOCK,
    )
    assert assessment.status == "unknown"


def test_generic_provider_date_is_never_temporal_authority() -> None:
    publication, signals = canonical_candidate_temporal(
        {"date": "2026-08-22T00:00:00Z"}
    )
    assert publication is None
    assert signals["provider_date"] == "2026-08-22T00:00:00Z"
    assessment = assess_candidate_temporal(
        {"published_at": publication, "date_signals": signals},
        _publication_spec(),
        now=CLOCK,
    )
    assert assessment.status == "unknown"


def test_document_jsonld_and_last_modified_are_distinct_signal_classes() -> None:
    html = b"""<html><head><script type="application/ld+json">
    {"@type":"LiveBlogPosting","datePublished":"2026-01-01T00:00:00Z",
     "dateModified":"2026-08-22T09:00:00Z"}
    </script></head><body>live updates</body></html>"""
    signals = extract_document_temporal_signals(
        html,
        mime_type="text/html",
        transport_metadata={
            "headers": {"Last-Modified": "Sat, 22 Aug 2026 10:00:00 GMT"}
        },
    )
    assert signals["published_at"].startswith("2026-01-01")
    assert signals["updated_at"] is None
    assert signals["update_status"] == "explicit_provider_conflict"
    assert {item["source"] for item in signals["update_signals"]} == {
        "document_json_ld:0:0",
        "http_header",
    }
