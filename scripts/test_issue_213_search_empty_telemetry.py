"""Regression coverage for issue #213 search and stage telemetry semantics."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from research_store.acquisition_service import AcquisitionResult
from research_store.fsearch_service import (
    FSearchError,
    FSearchRequest,
    FSearchService,
)

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
        SimpleNamespace(),
        SimpleNamespace(status=lambda **_kwargs: SimpleNamespace(id=run_id)),
        invocations,
        acquisition_factory=lambda: _AcquisitionService(status),
        direct_scrape_factory=lambda: pytest.fail(
            "empty or failed search must not construct direct scraping"
        ),
        preflight=lambda **_kwargs: SimpleNamespace(run_id=run_id),
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
