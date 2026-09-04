"""Regressions for issue #339 planned/candidate extraction-budget authority."""

from __future__ import annotations

from contextlib import AbstractContextManager
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from firecrawl_skill.research_domain import serialize_model
from firecrawl_skill.research_store.acquisition.candidate_ranking import (
    CandidateBudget,
    UrlType,
    check_corpus_budget,
    is_generic_url_type,
)
from firecrawl_skill.research_store.budget_policy import (
    DEFAULT_POLICY,
    conservative_research_spec,
)
from firecrawl_skill.research_store.candidate_budget_outcomes import (
    CandidateBudgetHardRejected,
    CandidateBudgetOverrideRequired,
)
from firecrawl_skill.research_store.candidate_policy_service import (
    CandidatePolicyError,
    CandidatePolicyService,
    pre_extraction_scope,
)
from firecrawl_skill.research_store.planned_acquisition import (
    DeterministicPlannedAcquisitionStage,
    DeterministicPlannedTemporalAcquisitionService,
)
from firecrawl_skill.research_store.run_budget_authority import (
    bind_planned_acquisition_budget_authority,
    load_persisted_candidate_budget,
    load_planned_extraction_attempt_limit,
)
from firecrawl_skill.research_store.smart_search_application import evaluate_budget
from firecrawl_skill.research_store.stages import StageOutcome


class _SearchResponses:
    def __init__(self, executed: list[str]) -> None:
        self.executed = executed

    def list_search_responses(self, run_id: UUID) -> list[dict[str, Any]]:
        del run_id
        return [{"query_text": value} for value in self.executed]


class _ExtractionAttempts:
    def __init__(self, attempted: int, succeeded: int) -> None:
        self.attempted = attempted
        self.succeeded = succeeded

    def count_for_run(self, run_id: UUID) -> int:
        del run_id
        return self.attempted

    def list_attempts_for_run(
        self,
        run_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        del run_id, limit, offset
        return [
            {"exit_status": "succeeded" if index < self.succeeded else "failed"}
            for index in range(self.attempted)
        ]


class _Uow(AbstractContextManager):
    def __init__(self, executed: list[str], attempted: int, succeeded: int) -> None:
        self.search_responses = _SearchResponses(executed)
        self.extraction_attempts = _ExtractionAttempts(attempted, succeeded)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


class _RunService:
    def __init__(self, *, attempted: int = 0, succeeded: int = 0) -> None:
        self.attempted = attempted
        self.succeeded = succeeded
        self.executed: list[str] = []
        self.transitions: list[tuple[Any, ...]] = []
        self.candidates: dict[UUID, dict[str, Any]] = {}
        self.occurrences: dict[UUID, list[dict[str, Any]]] = {}

    def uow_factory(self) -> _Uow:
        return _Uow(self.executed, self.attempted, self.succeeded)

    def seed_result(self, query_text: str, result: Any) -> None:
        if query_text not in self.executed:
            self.executed.append(query_text)
        for candidate in result.candidates:
            candidate_id = UUID(str(candidate["candidate_id"]))
            self.candidates[candidate_id] = dict(candidate)
            self.occurrences.setdefault(candidate_id, []).append(
                {
                    **candidate,
                    "search_response_id": result.search_response_id,
                }
            )

    def list_candidate_occurrences(
        self,
        candidate_id: UUID,
        *,
        run_id: UUID,
    ) -> list[dict[str, Any]]:
        del run_id
        return list(self.occurrences.get(candidate_id, ()))

    def get_candidate(self, candidate_id: UUID, *, run_id: UUID) -> dict[str, Any]:
        del run_id
        return self.candidates[candidate_id]

    def transition(self, *args: Any, **kwargs: Any) -> None:
        self.transitions.append((args, kwargs))


class _CoverageService:
    def apply_candidate_identified(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("coverage mutation is not expected in this regression")


class _AcquisitionService(DeterministicPlannedTemporalAcquisitionService):
    """Return more candidates than admitted to prove stage-side hard-cap defense."""

    def __init__(
        self,
        candidate_count: int = 20,
        *,
        url_template: str = "https://example.test/evidence/{index}",
    ) -> None:
        self.candidate_count = candidate_count
        self.url_template = url_template
        self.calls: list[dict[str, Any]] = []
        self.last_result: Any | None = None

    def execute_search(self, run_id: UUID, query_text: str, **kwargs: Any) -> Any:
        self.calls.append({"run_id": run_id, "query_text": query_text, **kwargs})
        candidates = []
        for index in range(self.candidate_count):
            candidate_id = uuid4()
            candidates.append(
                {
                    "id": uuid4(),
                    "candidate_id": candidate_id,
                    "canonical_url": self.url_template.format(index=index),
                    "raw_item": {},
                }
            )
        result = SimpleNamespace(
            search_response_id=uuid4(),
            candidate_count=len(candidates),
            candidates=candidates,
            search_response={},
        )
        self.last_result = result
        return result


class _CandidatePolicyService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.replay: dict[str, Any] | None = None
        self.overridden_limits: frozenset[str] = frozenset()
        self.check_id = uuid4()

    def accepted_pre_extraction_replay(
        self,
        run_id: UUID,
        lifecycle_revision: int,
    ) -> dict[str, Any] | None:
        del run_id, lifecycle_revision
        if self.replay is None or not self.overridden_limits:
            return None
        return self.replay

    def evaluate_pre_extraction(
        self,
        run_id: UUID,
        invocation_id: UUID | None,
        rankings: list[dict[str, Any]],
        selected_candidate_ids: list[UUID],
        budget: CandidateBudget,
        *,
        lifecycle_revision: int | None = None,
    ) -> Any:
        del invocation_id
        generic_page_count = sum(
            is_generic_url_type(UrlType(str(row["url_type"]))) for row in rankings
        )
        result = check_corpus_budget(
            (None,) * len(rankings),
            0,
            0,
            generic_page_count,
            len(selected_candidate_ids),
            {},
            budget=budget,
        )
        self.calls.append(
            {
                "run_id": run_id,
                "rankings": list(rankings),
                "selected_candidate_ids": list(selected_candidate_ids),
                "budget": budget,
                "lifecycle_revision": lifecycle_revision,
                "result": result,
            }
        )
        scope = pre_extraction_scope(rankings, selected_candidate_ids)
        self.replay = {
            "check_id": self.check_id,
            "scope": scope,
            "content_sha256": "f" * 64,
        }
        return SimpleNamespace(
            check_id=self.check_id,
            result=result,
            overridden_limits=self.overridden_limits,
            content_sha256="f" * 64,
        )


def _candidate_budget(
    max_attempts: int,
    *,
    max_candidates: int = 40,
) -> CandidateBudget:
    return CandidateBudget(
        max_candidates=max_candidates,
        max_exploratory_extraction_attempts=max_attempts,
    )


def _budget(
    spec,
    *,
    planning_attempts: int,
    candidate_attempts: int,
    candidate_max_candidates: int = 40,
) -> dict[str, Any]:
    snapshot = DEFAULT_POLICY.evaluate(
        spec,
        spec_revision=1,
        run_revision=0,
    ).to_dict()
    caps = snapshot["effective_caps"]
    caps["max_search_branches"] = 1
    caps["results_per_branch"] = 20
    caps["max_extraction_attempts"] = planning_attempts
    caps["max_successful_extractions"] = planning_attempts
    return bind_planned_acquisition_budget_authority(
        snapshot,
        _candidate_budget(
            candidate_attempts,
            max_candidates=candidate_max_candidates,
        ),
    )


def _context(
    *,
    planning_attempts: int,
    candidate_attempts: int,
    candidate_max_candidates: int = 40,
) -> dict[str, Any]:
    spec = conservative_research_spec("issue 339 budget authority", "general")
    return {
        "spec": serialize_model(spec),
        "search_plan_id": str(uuid4()),
        "authoritative_budget": _budget(
            spec,
            planning_attempts=planning_attempts,
            candidate_attempts=candidate_attempts,
            candidate_max_candidates=candidate_max_candidates,
        ),
        "search_plan": {
            "queries": [
                {
                    "query_id": str(uuid4()),
                    "query": "issue 339 authoritative evidence",
                    "facet": "authority",
                    "priority": 1,
                    "target_question_ids": [str(spec.questions[0].question_id)],
                    "target_claim_ids": [],
                }
            ]
        },
        "coverage_items": [],
    }


def _execute(
    *,
    planning_attempts: int,
    candidate_attempts: int,
    attempted: int = 0,
) -> tuple[Any, _AcquisitionService, dict[str, Any]]:
    run_service = _RunService(attempted=attempted)
    acquisition = _AcquisitionService()
    stage = DeterministicPlannedAcquisitionStage(
        run_service,
        acquisition,
        _CoverageService(),
        object(),
        SimpleNamespace(),
        candidate_policy_service=_CandidatePolicyService(),
    )
    context = _context(
        planning_attempts=planning_attempts,
        candidate_attempts=candidate_attempts,
    )
    result = stage.execute(
        uuid4(),
        run_revision=3,
        coverage_revision=None,
        run_state="acquiring",
        context=context,
    )
    return result, acquisition, context


@pytest.mark.parametrize(
    ("planning_attempts", "candidate_attempts"),
    [(18, 10), (10, 18)],
)
def test_stricter_extraction_authority_caps_planned_scheduling(
    planning_attempts: int,
    candidate_attempts: int,
) -> None:
    result, acquisition, context = _execute(
        planning_attempts=planning_attempts,
        candidate_attempts=candidate_attempts,
    )

    assert result.outcome is StageOutcome.CONTINUE
    assert len(acquisition.calls) == 1
    assert acquisition.calls[0]["selection_limit"] == 10
    assert context["effective_planned_extraction_attempt_cap"] == 10
    assert context["extraction_attempt_count"] == 10
    assert len(context["raw_ingest_requests"]) == 10


def test_restart_consumes_persisted_attempts_and_never_schedules_attempt_eleven() -> (
    None
):
    result, acquisition, context = _execute(
        planning_attempts=18,
        candidate_attempts=10,
        attempted=7,
    )

    assert result.outcome is StageOutcome.CONTINUE
    assert acquisition.calls[0]["selection_limit"] == 3
    assert context["extraction_attempt_count"] == 10
    assert len(context["raw_ingest_requests"]) == 3

    exhausted, exhausted_acquisition, exhausted_context = _execute(
        planning_attempts=18,
        candidate_attempts=10,
        attempted=10,
    )
    assert exhausted.outcome is StageOutcome.CONTINUE
    assert exhausted_acquisition.calls == []
    assert exhausted_context["extraction_attempt_count"] == 10
    assert exhausted_context["raw_ingest_requests"] == []


def test_restart_fails_closed_if_durable_attempts_already_exceed_reconciled_cap() -> (
    None
):
    result, acquisition, _context_value = _execute(
        planning_attempts=18,
        candidate_attempts=10,
        attempted=11,
    )

    assert result.outcome is StageOutcome.TERMINAL
    assert result.error is not None
    assert "reconciled planned acquisition budget" in result.error
    assert acquisition.calls == []


def test_planned_pre_extraction_rejects_non_extraction_hard_limit_before_handoff() -> (
    None
):
    run_service = _RunService()
    acquisition = _AcquisitionService(candidate_count=2)
    policy = _CandidatePolicyService()
    stage = DeterministicPlannedAcquisitionStage(
        run_service,
        acquisition,
        _CoverageService(),
        object(),
        SimpleNamespace(),
        candidate_policy_service=policy,
    )
    context = _context(
        planning_attempts=18,
        candidate_attempts=10,
        candidate_max_candidates=1,
    )

    with pytest.raises(CandidateBudgetHardRejected, match="max_candidates"):
        stage.execute(
            uuid4(),
            run_revision=3,
            coverage_revision=None,
            run_state="acquiring",
            context=context,
        )

    assert len(acquisition.calls) == 1
    assert run_service.transitions == []
    assert "raw_ingest_requests" not in context
    assert len(policy.calls) == 1
    assert policy.calls[0]["lifecycle_revision"] == 3
    assert policy.calls[0]["budget"].max_candidates == 1
    assert len(policy.calls[0]["rankings"]) == 2
    assert [item.limit_name for item in policy.calls[0]["result"].hard_violations] == [
        "max_candidates"
    ]


def test_planned_pre_extraction_requires_soft_override_before_handoff() -> None:
    run_service = _RunService()
    acquisition = _AcquisitionService(
        candidate_count=2,
        url_template="https://example.test/hub/topic-{index}",
    )
    policy = _CandidatePolicyService()
    stage = DeterministicPlannedAcquisitionStage(
        run_service,
        acquisition,
        _CoverageService(),
        object(),
        SimpleNamespace(),
        candidate_policy_service=policy,
    )
    context = _context(
        planning_attempts=18,
        candidate_attempts=10,
    )

    with pytest.raises(
        CandidateBudgetOverrideRequired,
        match="max_generic_page_share",
    ) as caught:
        stage.execute(
            uuid4(),
            run_revision=3,
            coverage_revision=None,
            run_state="acquiring",
            context=context,
        )

    assert caught.value.context.lifecycle_revision == 3
    assert caught.value.context.violated_limits == ("max_generic_page_share",)
    assert run_service.transitions == []
    assert "raw_ingest_requests" not in context
    assert len(policy.calls) == 1
    assert [item.limit_name for item in policy.calls[0]["result"].soft_violations] == [
        "max_generic_page_share"
    ]


def test_planned_soft_override_replays_exact_scope_without_new_search() -> None:
    run_id = uuid4()
    run_service = _RunService()
    acquisition = _AcquisitionService(
        candidate_count=2,
        url_template="https://example.test/hub/topic-{index}",
    )
    policy = _CandidatePolicyService()
    stage = DeterministicPlannedAcquisitionStage(
        run_service,
        acquisition,
        _CoverageService(),
        object(),
        SimpleNamespace(),
        candidate_policy_service=policy,
    )
    context = _context(planning_attempts=18, candidate_attempts=10)

    with pytest.raises(CandidateBudgetOverrideRequired):
        stage.execute(
            run_id,
            run_revision=3,
            coverage_revision=None,
            run_state="acquiring",
            context=context,
        )
    run_service.seed_result(
        "issue 339 authoritative evidence",
        acquisition.last_result,
    )
    policy.overridden_limits = frozenset({"max_generic_page_share"})

    resumed_context = _context(planning_attempts=18, candidate_attempts=10)
    result = stage.execute(
        run_id,
        run_revision=3,
        coverage_revision=None,
        run_state="acquiring",
        context=resumed_context,
    )

    assert result.outcome is StageOutcome.CONTINUE
    assert len(acquisition.calls) == 1
    assert len(policy.calls) == 2
    assert (
        policy.calls[1]["selected_candidate_ids"]
        == policy.calls[0]["selected_candidate_ids"]
    )
    assert resumed_context["extraction_attempt_count"] == 2
    assert len(resumed_context["raw_ingest_requests"]) == 2
    assert len(run_service.transitions) == 1
    assert run_service.transitions[0][0][1] == "extracting"


def test_configured_candidate_limit_is_snapshotted_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = conservative_research_spec("configured candidate budget", "general")
    monkeypatch.setenv("FIRECRAWL_BUDGET_MAX_EXTRACTION_ATTEMPTS", "7")
    snapshot = evaluate_budget(spec, 0)
    monkeypatch.setenv("FIRECRAWL_BUDGET_MAX_EXTRACTION_ATTEMPTS", "2")

    persisted = load_persisted_candidate_budget(snapshot)
    assert persisted.max_exploratory_extraction_attempts == 7
    assert load_planned_extraction_attempt_limit(snapshot) == min(
        int(snapshot["effective_caps"]["max_extraction_attempts"]),
        7,
    )


def test_candidate_policy_reuses_persisted_run_budget_after_runtime_change() -> None:
    spec = conservative_research_spec("completion budget restart", "general")
    snapshot = _budget(spec, planning_attempts=18, candidate_attempts=10)

    class _Runs:
        def get_latest_budget_snapshot(self, run_id: UUID) -> dict[str, Any]:
            del run_id
            return {"snapshot": snapshot}

    persisted = CandidatePolicyService._run_candidate_budget(
        SimpleNamespace(runs=_Runs()),
        uuid4(),
        _candidate_budget(3),
    )
    assert persisted.max_exploratory_extraction_attempts == 10

    admitted = check_corpus_budget((), 0, 0, 0, 10, {}, budget=persisted)
    rejected = check_corpus_budget((), 0, 0, 0, 11, {}, budget=persisted)
    assert admitted.accepted
    assert not rejected.accepted
    assert [item.limit_name for item in rejected.hard_violations] == [
        "max_exploratory_extraction_attempts"
    ]


def test_candidate_policy_fails_closed_on_incomplete_persisted_run_authority() -> None:
    spec = conservative_research_spec("incomplete candidate budget", "general")
    snapshot = DEFAULT_POLICY.evaluate(
        spec,
        spec_revision=1,
        run_revision=0,
    ).to_dict()

    class _Runs:
        def get_latest_budget_snapshot(self, run_id: UUID) -> dict[str, Any]:
            del run_id
            return {"snapshot": snapshot}

    with pytest.raises(CandidatePolicyError, match="no candidate_budget authority"):
        CandidatePolicyService._run_candidate_budget(
            SimpleNamespace(runs=_Runs()),
            uuid4(),
            CandidateBudget(),
        )
