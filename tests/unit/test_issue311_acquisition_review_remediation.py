"""Independent-review regressions for #311 acquisition authority."""

from __future__ import annotations

from contextlib import AbstractContextManager
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from firecrawl_skill.research_domain import serialize_model
from firecrawl_skill.research_store.budget_policy import DEFAULT_POLICY, conservative_research_spec
from firecrawl_skill.research_store.planned_acquisition import (
    DeterministicPlannedAcquisitionStage,
)


class _SearchResponses:
    def __init__(self, executed: list[str]) -> None:
        self.executed = executed

    def list_search_responses(self, run_id: UUID) -> list[dict[str, Any]]:
        del run_id
        return [{"query_text": value} for value in self.executed]


class _Uow(AbstractContextManager):
    def __init__(self, executed: list[str]) -> None:
        self.search_responses = _SearchResponses(executed)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


class _RunService:
    def __init__(self, executed: list[str]) -> None:
        self.executed = executed
        self.transitions: list[tuple[Any, ...]] = []

    def uow_factory(self) -> _Uow:
        return _Uow(self.executed)

    def transition(self, *args: Any, **kwargs: Any) -> None:
        self.transitions.append((args, kwargs))


class _CoverageService:
    def apply_candidate_identified(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("coverage mutation is not expected in this regression")


class _AcquisitionService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def execute_search(self, run_id: UUID, query_text: str, **kwargs: Any) -> Any:
        self.calls.append({"run_id": run_id, "query_text": query_text, **kwargs})
        return SimpleNamespace(
            search_response_id=uuid4(),
            candidate_count=0,
            candidates=[],
            search_response={},
        )


def _budget(spec) -> dict[str, Any]:
    snapshot = DEFAULT_POLICY.evaluate(
        spec,
        spec_revision=1,
        run_revision=0,
    ).to_dict()
    caps = snapshot["effective_caps"]
    caps["max_search_branches"] = 2
    caps["results_per_branch"] = 3
    caps["max_extraction_attempts"] = 2
    caps["max_successful_extractions"] = 2
    return snapshot


def test_planned_stage_uses_persisted_budget_restart_state_and_never_facet_scrape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from firecrawl_skill.research_store import planned_acquisition as module

    class _Reader:
        def __init__(self, _factory: Any) -> None:
            pass

        def attempt_census(self, run_id: UUID) -> Any:
            del run_id
            return SimpleNamespace(attempted=1, succeeded=0)

    monkeypatch.setattr(module, "PostgresResumeStateReader", _Reader)
    run_service = _RunService(executed=["alpha evidence"])
    acquisition = _AcquisitionService()
    stage = DeterministicPlannedAcquisitionStage(
        run_service,
        acquisition,
        _CoverageService(),
        object(),
        SimpleNamespace(),
    )
    spec = conservative_research_spec("review acquisition authority", "general")
    plan_id = uuid4()
    alpha_id = uuid4()
    beta_id = uuid4()
    context = {
        "spec": serialize_model(spec),
        "search_plan_id": str(plan_id),
        "authoritative_budget": _budget(spec),
        "search_plan": {
            "queries": [
                {
                    "query_id": str(alpha_id),
                    "query": "alpha evidence",
                    "facet": "authority",
                    "priority": 1,
                    "target_question_ids": [str(spec.questions[0].question_id)],
                    "target_claim_ids": [],
                },
                {
                    "query_id": str(beta_id),
                    "query": "beta evidence",
                    "facet": "benchmark_source",
                    "priority": 2,
                    "target_question_ids": [str(spec.questions[0].question_id)],
                    "target_claim_ids": [],
                },
            ]
        },
        "coverage_items": [],
    }

    result = stage.execute(
        uuid4(),
        run_revision=3,
        coverage_revision=None,
        run_state="acquiring",
        context=context,
    )

    assert result.success is True
    assert len(acquisition.calls) == 1
    call = acquisition.calls[0]
    assert call["query_text"] == "beta evidence"
    assert call["backend"] == "firecrawl"
    assert call["limit"] == 3
    assert call["selection_limit"] == 1
    assert call["plan_id"] == plan_id
    assert call["plan_query_id"] == beta_id


def test_planned_stage_fails_closed_without_persisted_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from firecrawl_skill.research_store import planned_acquisition as module

    class _Reader:
        def __init__(self, _factory: Any) -> None:
            pass

        def attempt_census(self, run_id: UUID) -> Any:
            del run_id
            return SimpleNamespace(attempted=0, succeeded=0)

    monkeypatch.setattr(module, "PostgresResumeStateReader", _Reader)
    run_service = _RunService(executed=[])
    acquisition = _AcquisitionService()
    stage = DeterministicPlannedAcquisitionStage(
        run_service,
        acquisition,
        _CoverageService(),
        object(),
        SimpleNamespace(),
    )
    spec = conservative_research_spec("missing budget", "general")
    context = {
        "spec": serialize_model(spec),
        "search_plan": {
            "queries": [
                {
                    "query_id": str(uuid4()),
                    "query": "missing budget",
                    "facet": "authority",
                    "priority": 1,
                }
            ]
        },
    }

    result = stage.execute(
        uuid4(),
        run_revision=1,
        coverage_revision=None,
        run_state="acquiring",
        context=context,
    )

    assert result.success is False
    assert "persisted authoritative_budget" in result.message
    assert acquisition.calls == []
