"""PostgreSQL regressions for issue #300 AC5 search temporal provenance."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.composition import (
    build_acquisition_service,
    build_run_service,
    build_workflow_operation_service,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.domain import SearchAdapterResult, utcnow
from firecrawl_skill.research_store.postgres import migrate

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


class _Adapter:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def search(self, query_text: str, **kwargs: Any) -> SearchAdapterResult:
        self.calls.append({"query": query_text, **kwargs})
        return SearchAdapterResult(
            raw_payload=json.dumps(self.payload).encode(),
            http_status=200,
            provider_request_id=f"issue300-{len(self.calls)}",
            requested_at=utcnow(),
            responded_at=utcnow(),
            transport_metadata={"attempts": 1},
        )


@pytest.fixture
def temporal_config(tmp_path: Path) -> StoreConfig:
    migrate(TEST_DSN)
    return replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection=f"issue300_temporal_{uuid4().hex}",
        embedding_dimension=4,
    )


def _prepared_run(config: StoreConfig):
    runs = build_run_service(config)
    external_id = f"fr_issue300_temporal_{uuid4().hex}"
    runs.create("issue 300 temporal acquisition", external_id)
    build_workflow_operation_service(config).prepare_run(external_id)
    return runs, runs.status(external_id=external_id)


def test_exact_recency_uses_provider_superset_and_persists_distinct_date_signals(
    temporal_config: StoreConfig,
) -> None:
    runs, status = _prepared_run(temporal_config)
    adapter = _Adapter(
        {
            "success": True,
            "data": {
                "web": [
                    {
                        "url": "https://example.test/dated",
                        "title": "Explicit temporal metadata",
                        "publishedDate": "2026-08-20T10:00:00Z",
                        "updatedAt": "2026-08-21T11:00:00Z",
                        "date": "2026-08-22T12:00:00Z",
                    }
                ]
            },
        }
    )
    service = build_acquisition_service(temporal_config, search_adapter=adapter)

    result = service.execute_search(status.id, "temporal metadata", tbs="qdr:5d")

    assert result.postgres_committed is True
    assert adapter.calls[0]["tbs"] == "qdr:w"
    assert result.search_response["recency"] == {
        "requested_tbs": "qdr:5d",
        "exact_seconds": 5 * 24 * 60 * 60,
        "exact_days": 5,
        "provider_tbs": "qdr:w",
        "authority": "local_exact_window",
    }
    candidate = runs.get_candidate(
        result.candidates[0]["candidate_id"], run_id=status.id
    )
    assert candidate["published_at"].isoformat().startswith("2026-08-20T10:00:00")
    signals = candidate["date_signals"]
    assert signals["published_date"].startswith("2026-08-20T10:00:00")
    assert signals["updated_date"] == "2026-08-21T11:00:00Z"
    assert signals["provider_date"] == "2026-08-22T12:00:00Z"
    assert signals["publication_status"] == "explicit_provider_valid"


def test_generic_provider_date_and_retrieval_do_not_become_publication(
    temporal_config: StoreConfig,
) -> None:
    runs, status = _prepared_run(temporal_config)
    adapter = _Adapter(
        {
            "success": True,
            "data": {
                "web": [
                    {
                        "url": "https://example.test/ambiguous",
                        "title": "Ambiguous date only",
                        "date": "2026-08-22T12:00:00Z",
                    }
                ]
            },
        }
    )
    service = build_acquisition_service(temporal_config, search_adapter=adapter)
    result = service.execute_search(status.id, "ambiguous temporal metadata")

    candidate = runs.get_candidate(
        result.candidates[0]["candidate_id"], run_id=status.id
    )
    assert candidate["published_at"] is None
    assert candidate["date_signals"]["provider_date"] == "2026-08-22T12:00:00Z"
    assert candidate["date_signals"]["publication_status"] == "unknown"
    assert "published_date" not in candidate["date_signals"]


def test_invalid_explicit_publication_is_unknown_not_generic_date_fallback(
    temporal_config: StoreConfig,
) -> None:
    runs, status = _prepared_run(temporal_config)
    adapter = _Adapter(
        {
            "success": True,
            "data": {
                "web": [
                    {
                        "url": "https://example.test/invalid-publication",
                        "publishedDate": "not-a-date",
                        "date": "2026-08-22T12:00:00Z",
                    }
                ]
            },
        }
    )
    service = build_acquisition_service(temporal_config, search_adapter=adapter)
    result = service.execute_search(status.id, "invalid publication")

    candidate = runs.get_candidate(
        result.candidates[0]["candidate_id"], run_id=status.id
    )
    assert candidate["published_at"] is None
    assert (
        candidate["date_signals"]["publication_status"] == "explicit_provider_invalid"
    )
    assert candidate["date_signals"]["publication_raw"] == "not-a-date"
    assert candidate["date_signals"]["provider_date"] == "2026-08-22T12:00:00Z"


def test_explicit_search_key_cannot_replay_different_exact_window_with_same_provider_filter(
    temporal_config: StoreConfig,
) -> None:
    from firecrawl_skill.research_store.acquisition.service import (
        AcquisitionIdempotencyConflictError,
    )

    _runs, status = _prepared_run(temporal_config)
    adapter = _Adapter(
        {
            "success": True,
            "data": {"web": [{"url": "https://example.test/exact-replay"}]},
        }
    )
    service = build_acquisition_service(temporal_config, search_adapter=adapter)
    key = "issue300:exact-recency-replay"

    service.execute_search(status.id, "exact replay", tbs="qdr:5d", idempotency_key=key)
    with pytest.raises(AcquisitionIdempotencyConflictError, match="exact recency"):
        service.execute_search(
            status.id,
            "exact replay",
            tbs="qdr:7d",
            idempotency_key=key,
        )

    assert len(adapter.calls) == 1
    assert adapter.calls[0]["tbs"] == "qdr:w"
