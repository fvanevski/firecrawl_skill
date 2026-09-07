"""Issue #367 regressions for canonical markdown temporal provenance."""

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


def _service(candidate):
    return TemporalCorpusService(_Delegate(), lambda: _Uow(candidate))


def test_markdown_explicit_publication_marker_is_authoritative() -> None:
    signals = extract_document_temporal_signals(
        b"# Story\n\nPublished on September 5, 2026\n",
        mime_type="text/markdown",
    )

    assert signals["publication_status"] == "explicit_provider_valid"
    assert signals["published_at"] == "2026-09-05T00:00:00+00:00"
    assert signals["update_status"] == "unknown"
    assert signals["publication_signals"] == [
        {
            "signal_class": "publication",
            "source": "markdown_explicit_marker",
            "field": "published",
            "raw": "September 5, 2026",
            "parsed": "2026-09-05T00:00:00+00:00",
            "status": "valid",
        }
    ]


def test_markdown_explicit_update_is_not_promoted_to_publication() -> None:
    signals = extract_document_temporal_signals(
        b"# Story\n\nLast updated: September 6, 2026\n",
        mime_type="text/markdown",
    )

    assert signals["publication_status"] == "unknown"
    assert signals["published_at"] is None
    assert signals["update_status"] == "explicit_provider_valid"
    assert signals["updated_at"] == "2026-09-06T00:00:00+00:00"
    assert signals["update_signals"][0]["source"] == "markdown_explicit_marker"


def test_github_opened_marker_requires_issue_or_pr_source_context() -> None:
    source_url = "https://github.com/fvanevski/firecrawl_skill/issues/367"
    signals = extract_document_temporal_signals(
        (
            "[fvanevski](https://github.com/fvanevski)\n"
            "fvanevski opened on September 7, 2026\n"
            "Issue body actions\n"
        ).encode(),
        mime_type="text/markdown",
        source_context={"final_url": source_url},
    )

    assert signals["publication_status"] == "explicit_provider_valid"
    assert signals["published_at"] == "2026-09-07T00:00:00+00:00"
    signal = signals["publication_signals"][0]
    assert signal["source"] == "github_issue_pr_opened_marker"
    assert signal["field"] == "opened_on"
    assert signal["context"] == {
        "source_kind": "github_issue_or_pr",
        "source_url": source_url,
    }


def test_github_link_wrapped_opened_marker_matches_canonical_issue_anchor() -> None:
    source_url = "https://github.com/vllm-project/vllm/issues/45273"
    signals = extract_document_temporal_signals(
        (
            "[vllm-user](https://github.com/vllm-user)\n"
            "opened [on Jun 11, 2026]"
            "(https://github.com/vllm-project/vllm/issues/45273#issue-4640307152)\n"
            "Issue body actions\n"
        ).encode(),
        mime_type="text/markdown",
        source_context={"final_url": source_url},
    )

    assert signals["publication_status"] == "explicit_provider_valid"
    assert signals["published_at"] == "2026-06-11T00:00:00+00:00"
    signal = signals["publication_signals"][0]
    assert signal["source"] == "github_issue_pr_opened_marker"
    assert signal["field"] == "opened_on"
    assert signal["raw"] == "Jun 11, 2026"
    assert signal["context"]["source_url"] == source_url


def test_github_link_wrapped_opened_marker_rejects_mismatched_anchor() -> None:
    source_url = "https://github.com/vllm-project/vllm/issues/45273"
    signals = extract_document_temporal_signals(
        (
            "[vllm-user](https://github.com/vllm-user)\n"
            "opened [on Jun 11, 2026]"
            "(https://github.com/vllm-project/vllm/issues/45274#issue-4640307152)\n"
            "Issue body actions\n"
        ).encode(),
        mime_type="text/markdown",
        source_context={"final_url": source_url},
    )

    assert signals["publication_status"] == "unknown"
    assert signals["published_at"] is None
    assert signals["publication_signals"] == []


def test_github_user_authored_opened_marker_after_body_boundary_is_not_authority() -> None:
    source_url = "https://github.com/vllm-project/vllm/issues/45273"
    signals = extract_document_temporal_signals(
        (
            "[vllm-user](https://github.com/vllm-user)\n"
            "Issue body actions\n"
            "[attacker](https://github.com/attacker)\n"
            "opened [on Sep 1, 2026]"
            "(https://github.com/vllm-project/vllm/issues/45273#issue-4640307152)\n"
            "Issue body actions\n"
        ).encode(),
        mime_type="text/markdown",
        source_context={"final_url": source_url},
    )

    assert signals["publication_status"] == "unknown"
    assert signals["published_at"] is None
    assert signals["publication_signals"] == []


def test_markdown_temporal_markers_inside_fenced_code_are_not_authority() -> None:
    signals = extract_document_temporal_signals(
        b"```text\nPublished on September 5, 2026\nLast updated: September 6, 2026\n```\n",
        mime_type="text/markdown",
    )

    assert signals["publication_status"] == "unknown"
    assert signals["update_status"] == "unknown"
    assert signals["publication_signals"] == []
    assert signals["update_signals"] == []


def test_markdown_truncated_final_line_is_not_temporal_authority() -> None:
    marker = "Published on September 7, 2026"
    padding = "x" * (262_144 - len(marker) - 1)
    content = (padding + "\n" + marker + " garbage\n").encode()

    signals = extract_document_temporal_signals(content, mime_type="text/markdown")

    assert signals["publication_status"] == "unknown"
    assert signals["published_at"] is None
    assert signals["publication_signals"] == []


def test_github_opened_phrase_in_general_markdown_is_not_authority() -> None:
    contexts = (
        {"final_url": "https://example.test/article"},
        {"final_url": "https://github.com/fvanevski/firecrawl_skill"},
        {
            "final_url": "https://example.test/redirected",
            "requested_url": "https://github.com/fvanevski/firecrawl_skill/issues/367",
        },
        {
            "final_url": "https://[malformed",
            "requested_url": "https://github.com/fvanevski/firecrawl_skill/issues/367",
        },
    )
    for source_context in contexts:
        signals = extract_document_temporal_signals(
            (
                "[fvanevski](https://github.com/fvanevski)\n"
                "fvanevski opened on September 7, 2026\n"
                "Issue body actions\n"
            ).encode(),
            mime_type="text/markdown",
            source_context=source_context,
        )

        assert signals["publication_status"] == "unknown"
        assert signals["published_at"] is None
        assert signals["publication_signals"] == []


def test_invalid_explicit_markdown_time_fails_closed() -> None:
    signals = extract_document_temporal_signals(
        b"Published on definitely-not-a-date\n",
        mime_type="text/markdown",
    )

    assert signals["publication_status"] == "explicit_provider_invalid"
    assert signals["published_at"] is None
    assert signals["publication_signals"][0]["status"] == "invalid"
    assert signals["publication_signals"][0]["raw"] == "definitely-not-a-date"


def test_markdown_candidate_document_conflict_remains_fail_closed() -> None:
    candidate_id = uuid4()
    candidate = {
        "id": candidate_id,
        "published_at": datetime(2026, 9, 6, tzinfo=timezone.utc),
        "date_signals": {
            "publication_status": "explicit_provider_valid",
            "update_status": "unknown",
        },
    }
    request = IngestRequest(
        "https://example.test/story",
        b"Published on September 7, 2026\n",
        mime_type="text/markdown",
        metadata={"candidate_id": str(candidate_id)},
    )

    prepared = _service(candidate).prepare_ingest(request)
    provenance = prepared.metadata["temporal_provenance"]

    assert prepared.published_at is None
    assert provenance["publication_status"] == "explicit_conflict"
    assert provenance["document_publication_status"] == "explicit_provider_valid"
    assert provenance["publication_authority"] == "none"


def test_temporal_corpus_supplies_github_source_context_from_request() -> None:
    candidate_id = uuid4()
    source_url = "https://github.com/fvanevski/firecrawl_skill/issues/367"
    candidate = {
        "id": candidate_id,
        "published_at": None,
        "date_signals": {
            "publication_status": "unknown",
            "update_status": "unknown",
        },
    }
    request = IngestRequest(
        source_url,
        (
            "[fvanevski](https://github.com/fvanevski)\n"
            "opened [on September 7, 2026]"
            "(https://github.com/fvanevski/firecrawl_skill/issues/367#issue-9999999999)\n"
            "Issue body actions\n"
        ).encode(),
        final_url=source_url,
        mime_type="text/markdown",
        metadata={
            "candidate_id": str(candidate_id),
            "firecrawl": {"source_url": source_url},
        },
    )

    prepared = _service(candidate).prepare_ingest(request)
    provenance = prepared.metadata["temporal_provenance"]

    assert prepared.published_at == datetime(2026, 9, 7, tzinfo=timezone.utc)
    assert provenance["publication_status"] == "explicit_valid"
    assert provenance["publication_authority"] == "explicit_signal_only"
    assert provenance["document_publication_status"] == "explicit_provider_valid"
    assert provenance["document_publication_signals"][0]["context"]["source_url"] == (
        source_url
    )


def test_provider_date_retrieval_and_first_seen_remain_non_authoritative() -> None:
    candidate_id = uuid4()
    candidate = {
        "id": candidate_id,
        "published_at": None,
        "first_seen_at": datetime(2026, 9, 7, 8, tzinfo=timezone.utc),
        "date_signals": {
            "publication_status": "unknown",
            "update_status": "unknown",
            "provider_date": "2026-09-07T08:00:00Z",
        },
    }
    request = IngestRequest(
        "https://example.test/undated",
        b"# Undated source\n",
        mime_type="text/markdown",
        retrieved_at=datetime(2026, 9, 7, 9, tzinfo=timezone.utc),
        metadata={"candidate_id": str(candidate_id)},
    )

    prepared = _service(candidate).prepare_ingest(request)
    provenance = prepared.metadata["temporal_provenance"]

    assert prepared.published_at is None
    assert provenance["publication_status"] == "unknown"
    assert provenance["document_publication_status"] == "unknown"
    assert provenance["candidate_publication_signals"] == []
    assert provenance["retrieved_at"] == "2026-09-07T09:00:00+00:00"
    assert provenance["retrieval_is_publication"] is False
