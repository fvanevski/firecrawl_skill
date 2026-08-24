"""Issue #307 completion regressions for explicit document temporal provenance."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from firecrawl_skill.research_store.domain import IngestRequest
from firecrawl_skill.research_store.temporal_candidate import (
    extract_document_temporal_signals,
)
from firecrawl_skill.research_store.temporal_corpus import TemporalCorpusService


class _Candidates:
    def __init__(self, candidate):
        self.candidate = candidate

    def get_candidate(self, candidate_id):
        assert candidate_id == self.candidate["id"]
        return self.candidate


class _Uow:
    def __init__(self, candidate):
        self.candidates = _Candidates(candidate)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Delegate:
    def prepare_ingest(self, request):
        return request


def test_page_visible_updated_marker_is_retained_as_update_not_publication() -> None:
    html = b"""<html><body><p>Updated August 22, 2026 at 9:30 PM</p><p>Story text.</p></body></html>"""
    signals = extract_document_temporal_signals(html, mime_type="text/html")

    assert signals["published_at"] is None
    assert signals["updated_at"].startswith("2026-08-22T21:30:00")
    assert signals["update_status"] == "explicit_provider_valid"
    assert signals["update_signals"][0]["source"] == "page_text_explicit_marker"


def test_html_meta_publication_and_update_are_distinct() -> None:
    html = b"""<html><head>
    <meta property="article:published_time" content="2026-08-20T10:00:00Z">
    <meta property="article:modified_time" content="2026-08-22T11:00:00Z">
    </head><body>Story</body></html>"""
    signals = extract_document_temporal_signals(html, mime_type="text/html")

    assert signals["published_at"].startswith("2026-08-20T10:00:00")
    assert signals["updated_at"].startswith("2026-08-22T11:00:00")
    assert {item["signal_class"] for item in signals["publication_signals"]} == {
        "publication"
    }
    assert {item["signal_class"] for item in signals["update_signals"]} == {"update"}


def test_nested_live_blog_posts_keep_segment_provenance_and_fail_page_level_closed() -> (
    None
):
    html = b"""<html><head><script type="application/ld+json">
    {"@type":"LiveBlogPosting","headline":"Live coverage","liveBlogUpdate":[
      {"@type":"BlogPosting","headline":"First","datePublished":"2026-08-22T09:00:00Z","dateModified":"2026-08-22T09:30:00Z"},
      {"@type":"BlogPosting","headline":"Second","datePublished":"2026-08-22T10:00:00Z","dateModified":"2026-08-22T10:15:00Z"}
    ]}
    </script></head><body>Live updates</body></html>"""
    signals = extract_document_temporal_signals(html, mime_type="text/html")

    assert signals["published_at"] is None
    assert signals["updated_at"] is None
    assert signals["publication_status"] == "explicit_provider_conflict"
    assert signals["update_status"] == "explicit_provider_conflict"
    segments = signals["structured_temporal_segments"]
    assert [item["headline"] for item in segments] == ["First", "Second"]
    assert all(
        item["publication_status"] == "explicit_provider_valid" for item in segments
    )
    assert all(item["update_status"] == "explicit_provider_valid" for item in segments)


def test_equivalent_offset_candidate_and_document_signals_corroborate() -> None:
    candidate_id = uuid4()
    candidate = {
        "id": candidate_id,
        "published_at": datetime(2026, 8, 22, 10, tzinfo=timezone.utc),
        "date_signals": {
            "publication_status": "explicit_provider_valid",
            "update_status": "explicit_provider_valid",
            "updated_date": "2026-08-22T11:00:00Z",
        },
    }
    service = TemporalCorpusService(_Delegate(), lambda: _Uow(candidate))
    request = IngestRequest(
        "https://example.test/equivalent-offsets",
        b"""<html><head>
        <meta property="article:published_time" content="2026-08-22T06:00:00-04:00">
        <meta property="article:modified_time" content="2026-08-22T07:00:00-04:00">
        </head><body>Story</body></html>""",
        mime_type="text/html",
        retrieved_at=datetime(2026, 8, 23, 12, tzinfo=timezone.utc),
        metadata={"candidate_id": str(candidate_id)},
    )

    prepared = service.prepare_ingest(request)
    provenance = prepared.metadata["temporal_provenance"]

    assert prepared.published_at == datetime(2026, 8, 22, 10, tzinfo=timezone.utc)
    assert prepared.last_modified == "2026-08-22T11:00:00+00:00"
    assert provenance["publication_status"] == "explicit_valid"
    assert provenance["update_status"] == "explicit_valid"
    assert provenance["publication_authority"] == "multiple_consistent_explicit"
    assert provenance["update_authority"] == "multiple_consistent_explicit"


def test_candidate_and_document_update_conflict_fails_closed() -> None:
    candidate_id = uuid4()
    candidate = {
        "id": candidate_id,
        "published_at": datetime(2026, 8, 20, 10, tzinfo=timezone.utc),
        "date_signals": {
            "publication_status": "explicit_provider_valid",
            "update_status": "explicit_provider_valid",
            "updated_date": "2026-08-21T11:00:00Z",
        },
    }
    service = TemporalCorpusService(_Delegate(), lambda: _Uow(candidate))
    request = IngestRequest(
        "https://example.test/conflicting-update",
        b"""<html><head><meta property="article:modified_time" content="2026-08-22T11:00:00Z"></head><body>Story</body></html>""",
        mime_type="text/html",
        retrieved_at=datetime(2026, 8, 23, 12, tzinfo=timezone.utc),
        metadata={"candidate_id": str(candidate_id)},
    )

    prepared = service.prepare_ingest(request)
    provenance = prepared.metadata["temporal_provenance"]

    assert prepared.last_modified is None
    assert provenance["updated_at"] is None
    assert provenance["update_status"] == "explicit_conflict"
    assert provenance["update_authority"] == "none"


def test_candidate_conflict_is_not_masked_by_valid_document_publication() -> None:
    candidate_id = uuid4()
    candidate = {
        "id": candidate_id,
        "published_at": None,
        "date_signals": {
            "publication_status": "explicit_provider_conflict",
            "update_status": "unknown",
        },
    }
    service = TemporalCorpusService(_Delegate(), lambda: _Uow(candidate))
    request = IngestRequest(
        "https://example.test/conflicting-publication",
        b"""<html><head><meta property="article:published_time" content="2026-08-22T11:00:00Z"></head><body>Story</body></html>""",
        mime_type="text/html",
        metadata={"candidate_id": str(candidate_id)},
    )

    prepared = service.prepare_ingest(request)

    assert prepared.published_at is None
    assert prepared.metadata["temporal_provenance"]["publication_status"] == (
        "explicit_conflict"
    )
