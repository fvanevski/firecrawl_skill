"""Regression coverage for issue #213 search and stage telemetry semantics."""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from research_store.acquisition.authority import AuthoritativeAcquisitionContext
from research_store.acquisition_service import AcquisitionResult, AcquisitionService
from research_store.config import StoreConfig
from research_store.fsearch_service import (
    FSearchError,
    FSearchRequest,
    FSearchService,
)
from research_store.parsing import parse_raw_search_response

RUN_EXTERNAL_ID = "fr_" + "a" * 32


class _InvocationService:
    def __init__(self) -> None:
        self.completed: list[tuple[str, dict]] = []

    def begin(self, *_args, **_kwargs):
        return SimpleNamespace(id=uuid4())

    def complete(self, _run_id, _invocation_id, status, **kwargs):
        self.completed.append((status, kwargs))
        return SimpleNamespace(id=_invocation_id)


class _AcquisitionService:
    def __init__(self, status: str) -> None:
        self.status = status

    def execute_search(self, run_id, query_text, **_kwargs) -> AcquisitionResult:
        return AcquisitionResult(
            search_response_id=uuid4(),
            run_id=run_id,
            query_text=query_text,
            backend="firecrawl",
            status=self.status,
            candidate_count=0,
            candidates=[],
            postgres_committed=True,
            search_response={"error_message": "provider diagnostic"},
        )


def _service(status: str) -> tuple[FSearchService, _InvocationService]:
    run_id = uuid4()
    invocations = _InvocationService()
    service = FSearchService(
        cast(StoreConfig, SimpleNamespace()),
        SimpleNamespace(status=lambda **_kwargs: SimpleNamespace(id=run_id)),
        invocations,
        acquisition_factory=cast(
            Callable[[], AcquisitionService], lambda: _AcquisitionService(status)
        ),
        direct_scrape_factory=lambda: pytest.fail(
            "empty or failed search must not construct direct scraping"
        ),
        preflight=cast(
            Callable[..., AuthoritativeAcquisitionContext],
            lambda **_kwargs: SimpleNamespace(run_id=run_id),
        ),
        classify_target=lambda *_args: ("other", False),
        profiles={"news_article": {"target_schema": {"type": "object"}}},
    )
    return service, invocations


def test_empty_search_completes_invocation_successfully() -> None:
    service, invocations = _service("empty")

    result = service.execute(FSearchRequest("test query", RUN_EXTERNAL_ID))

    assert result.status == "empty"
    assert result.search_response_id is not None
    assert result.candidate_ids == ()
    assert [status for status, _kwargs in invocations.completed] == ["succeeded"]


@pytest.mark.parametrize(
    ("status", "expected_stage"),
    [
        ("provider_error", "search_transport"),
        ("parse_error", "candidate_parsing"),
    ],
)
def test_search_failures_remain_failed_invocations(
    status: str,
    expected_stage: str,
) -> None:
    service, invocations = _service(status)

    with pytest.raises(FSearchError) as caught:
        service.execute(FSearchRequest("test query", RUN_EXTERNAL_ID))

    assert caught.value.stage == expected_stage
    assert caught.value.result is not None
    assert caught.value.result.search_response_id is not None
    assert [terminal for terminal, _kwargs in invocations.completed] == ["failed"]


def test_no_results_marker_rejects_nonempty_secondary_collection() -> None:
    status, count, summary, error = parse_raw_search_response(
        json.dumps(
            {
                "error": "No results found",
                "data": [],
                "results": [{"url": "https://example.test/result"}],
            }
        )
    )

    assert status == "parse_error"
    assert count == 0
    assert summary["keys"] == ["data", "error", "results"]
    assert error == (
        "Provider no-results response violated the supported empty-result contract"
    )


@pytest.mark.parametrize(
    ("payload", "expected_status", "expected_count"),
    [
        (
            {
                "success": True,
                "data": None,
                "results": [{"url": "https://example.test/result"}],
            },
            "succeeded",
            1,
        ),
        (
            {"success": True, "data": None, "results": []},
            "empty",
            0,
        ),
    ],
)
def test_result_envelope_falls_through_unusable_earlier_alias(
    payload: dict,
    expected_status: str,
    expected_count: int,
) -> None:
    status, count, _summary, error = parse_raw_search_response(json.dumps(payload))

    assert status == expected_status
    assert count == expected_count
    assert error is None


def test_no_results_marker_requires_every_declared_collection_to_be_valid() -> None:
    status, count, summary, error = parse_raw_search_response(
        json.dumps(
            {
                "error": "No results found",
                "data": None,
                "results": [],
            }
        )
    )

    assert status == "parse_error"
    assert count == 0
    assert summary["keys"] == ["data", "error", "results"]
    assert error == (
        "Provider no-results response violated the supported empty-result contract"
    )


def test_no_results_marker_accepts_multiple_explicitly_empty_collections() -> None:
    status, count, summary, error = parse_raw_search_response(
        json.dumps(
            {
                "error": "No results found",
                "data": [],
                "results": [],
            }
        )
    )

    assert status == "empty"
    assert count == 0
    assert summary == {"result_count": 0, "sample_candidates": []}
    assert error is None
