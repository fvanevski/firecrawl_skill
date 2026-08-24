"""Issue #300 temporal provenance propagation into direct-scrape ingestion."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from firecrawl_skill.research_store.domain import IngestRequest
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
    def __init__(self):
        self.requests = []

    def prepare_ingest(self, request):
        self.requests.append(request)
        return request


class _BatchDelegate:
    def __init__(self):
        self.batches: list[list] = []
        self.requests: list = []

    def prepare_ingest(self, request):
        self.requests.append(request)
        return request

    def ingest_batch(
        self,
        invocation_id,
        operation,
        requests,
        *,
        research_run_external_id=None,
        metadata=None,
    ):
        self.batches.append(list(requests))
        return {"batch_id": "b2"}

    def bounded_ingest_batch(
        self,
        invocation_id,
        operation,
        requests,
        *,
        research_run_external_id=None,
        metadata=None,
    ):
        self.batches.append(list(requests))
        return {"batch_id": "b1"}


def test_search_publication_and_update_are_carried_without_retrieval_inference() -> (
    None
):
    candidate_id = uuid4()
    published = datetime(2026, 8, 20, 10, tzinfo=timezone.utc)
    candidate = {
        "id": candidate_id,
        "published_at": published,
        "date_signals": {"updated_date": "2026-08-21T11:00:00Z"},
    }
    delegate = _Delegate()
    service = TemporalCorpusService(delegate, lambda: _Uow(candidate))
    retrieved = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    request = IngestRequest(
        "https://example.test/temporal",
        b"content",
        retrieved_at=retrieved,
        metadata={"direct_scrape": {"candidate_id": str(candidate_id)}},
    )

    prepared = service.prepare_ingest(request)

    assert prepared.published_at == published
    assert prepared.last_modified == "2026-08-21T11:00:00+00:00"
    provenance = prepared.metadata["temporal_provenance"]
    assert provenance["published_at"] == published.isoformat()
    assert provenance["updated_at"] == "2026-08-21T11:00:00+00:00"
    assert provenance["retrieved_at"] == retrieved.isoformat()
    assert provenance["retrieval_is_publication"] is False
    assert provenance["publication_authority"] == "explicit_provider_only"


def test_candidate_without_publication_does_not_promote_retrieval_time() -> None:
    candidate_id = uuid4()
    candidate = {"id": candidate_id, "published_at": None, "date_signals": {}}
    delegate = _Delegate()
    service = TemporalCorpusService(delegate, lambda: _Uow(candidate))
    request = IngestRequest(
        "https://example.test/retrieval-only",
        b"content",
        retrieved_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        metadata={"direct_scrape": {"candidate_id": str(candidate_id)}},
    )

    prepared = service.prepare_ingest(request)

    assert prepared.published_at is None
    assert prepared.last_modified is None
    assert prepared.metadata["temporal_provenance"]["published_at"] is None


def test_orchestrator_batch_metadata_carries_provider_provenance() -> None:
    candidate_id = uuid4()
    published = datetime(2026, 8, 20, 10, tzinfo=timezone.utc)
    candidate = {
        "id": candidate_id,
        "published_at": published,
        "date_signals": {"updated_date": "2026-08-21T11:00:00Z"},
    }
    delegate = _BatchDelegate()
    service = TemporalCorpusService(delegate, lambda: _Uow(candidate))
    request = IngestRequest(
        "https://example.test/orchestrator",
        b"content",
        retrieved_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        metadata={"candidate_id": str(candidate_id)},
    )
    failed_item = {"error": "acquisition failed"}
    missing_item = {"request": None, "error": "scrape timeout"}

    service.bounded_ingest_batch(
        "issue300-batch",
        "extraction",
        [request, failed_item, missing_item],
    )

    (batch,) = delegate.batches
    enriched, untouched, still_missing = batch
    assert isinstance(enriched, IngestRequest)
    assert enriched.published_at == published
    assert enriched.last_modified == "2026-08-21T11:00:00+00:00"
    assert enriched.metadata["temporal_provenance"]["publication_authority"] == (
        "explicit_provider_only"
    )
    assert enriched.metadata["temporal_provenance"]["retrieval_is_publication"] is False
    assert untouched is failed_item
    assert still_missing is missing_item


def test_batch_items_without_candidate_provenance_are_forwarded_untouched() -> None:
    delegate = _BatchDelegate()
    service = TemporalCorpusService(delegate, lambda: _Uow({}))
    request = IngestRequest(
        "https://example.test/no-candidate",
        b"content",
        retrieved_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        metadata={"firecrawl": {"result_index": 0}},
    )

    service.ingest_batch("issue300-plain", "direct_scrape", [request])

    (batch,) = delegate.batches
    assert batch[0] is request
