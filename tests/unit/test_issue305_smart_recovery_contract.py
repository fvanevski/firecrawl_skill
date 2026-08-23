from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from firecrawl_skill.research_store.candidate_budget_outcomes import (
    CandidateBudgetAdmissionContext,
    CandidateBudgetHardRejected,
    CandidateBudgetOverrideRequired,
    classify_persisted_completion_admission,
)
from firecrawl_skill.research_store.orchestration import resume as resume_module
from firecrawl_skill.research_store.orchestration.commands import RunResearchCommand
from firecrawl_skill.research_store.orchestration.ports import ResumeCounts
from firecrawl_skill.research_store.orchestration.resume import run_resume
from firecrawl_skill.research_store.orchestrator import OrchestratorResult
from firecrawl_skill.research_store.smart_result import (
    SMART_FAILURE_EXIT,
    SMART_RESUMABLE_EXIT,
    SMART_SUCCESS_EXIT,
    OperatorActionOrchestratorResult,
    smart_cli_disposition,
)


@pytest.mark.parametrize(
    ("state", "outcome", "error", "expected"),
    [
        ("completed", "completed", None, SMART_SUCCESS_EXIT),
        ("partial", "partial", None, SMART_SUCCESS_EXIT),
        ("failed", "failed", None, SMART_FAILURE_EXIT),
        ("cancelled", "cancelled", None, SMART_FAILURE_EXIT),
        ("indexing", "checkpoint", None, SMART_RESUMABLE_EXIT),
        ("indexing", "resumable", None, SMART_RESUMABLE_EXIT),
        ("indexing", "operator_action_required", None, SMART_RESUMABLE_EXIT),
        ("failed", "failed", "boom", SMART_FAILURE_EXIT),
    ],
)
def test_canonical_smart_result_to_exit_mapping(
    state: str, outcome: str, error: str | None, expected: int
) -> None:
    result = OrchestratorResult(
        run_id=uuid4(), final_state=state, outcome=outcome, error=error
    )
    assert smart_cli_disposition(result).exit_code == expected


def test_unknown_nonterminal_result_fails_closed() -> None:
    result = OrchestratorResult(
        run_id=uuid4(), final_state="indexing", outcome="mystery", error=None
    )
    disposition = smart_cli_disposition(result)
    assert disposition.exit_code == SMART_FAILURE_EXIT
    assert disposition.next_action == "inspect_unrecognized_result"


class _Policy:
    def __init__(self, checks):
        self._checks = checks

    def list_checks(self, run_id: UUID):
        return self._checks


def _check(*, hard=(), soft=(), overridden=()):
    return {
        "id": str(uuid4()),
        "phase": "completion_admission",
        "lifecycle_revision": 7,
        "scope": {"subject_ids": [str(uuid4())]},
        "content_sha256": "a" * 64,
        "hard_violations": [{"limit_name": name} for name in hard],
        "soft_violations": [{"limit_name": name} for name in soft],
        "overridden_limits": list(overridden),
    }


def test_soft_completion_admission_is_typed_from_persisted_check() -> None:
    run_id = uuid4()
    outcome = classify_persisted_completion_admission(
        _Policy([_check(soft=("max_generic_page_share",))]), run_id, 7
    )
    assert isinstance(outcome, CandidateBudgetOverrideRequired)
    assert outcome.context.run_id == run_id
    assert outcome.context.lifecycle_revision == 7
    assert outcome.context.scope_fingerprint == "a" * 64
    assert outcome.context.violated_limits == ("max_generic_page_share",)


def test_hard_completion_admission_is_distinct_and_non_overridable() -> None:
    outcome = classify_persisted_completion_admission(
        _Policy([_check(hard=("max_total_bytes",), soft=("max_generic_page_share",))]),
        uuid4(),
        7,
    )
    assert isinstance(outcome, CandidateBudgetHardRejected)
    assert outcome.context.violated_limits == ("max_total_bytes",)


def test_exact_soft_override_removes_operator_action() -> None:
    outcome = classify_persisted_completion_admission(
        _Policy(
            [
                _check(
                    soft=("max_generic_page_share",),
                    overridden=("max_generic_page_share",),
                )
            ]
        ),
        uuid4(),
        7,
    )
    assert outcome is None


def test_stale_revision_check_cannot_authorize_current_revision() -> None:
    outcome = classify_persisted_completion_admission(
        _Policy([_check(soft=("max_generic_page_share",))]), uuid4(), 8
    )
    assert outcome is None


class _ResumeState:
    def counts(self, _run_id: UUID) -> ResumeCounts:
        return ResumeCounts(waves=2, attempts=3, assets=1)

    def authorized_queries(self, _run_id: UUID) -> list[dict[str, Any]]:
        return []

    def completed_candidates(self, _run_id: UUID) -> set[str]:
        return set()

    def assets(self, _run_id: UUID) -> list[dict[str, Any]]:
        return [{"chunk_ids": [str(uuid4())]}]

    def packet_revision(self, _run_id: UUID) -> int:
        return 1


class _AdmissionOrchestrator:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.failed_errors: list[str] = []
        self.orchestrator_config = SimpleNamespace(
            max_adaptive_cycles=4,
            execution_mode="autonomous_local",
        )

    def _refresh(self, _run_id: UUID) -> tuple[str, int]:
        return "indexing", 7

    def _execute_stage(self, stage_name: str, *_args: Any) -> Any:
        assert stage_name == "indexing"
        raise self.failure

    def _checkpoint(self, *_args: Any) -> None:
        return None

    def _failed_result(self, run_id: UUID, error: str) -> OrchestratorResult:
        self.failed_errors.append(error)
        return OrchestratorResult(
            run_id=run_id,
            final_state="failed",
            outcome="failed",
            error=error,
        )


def _admission_context(run_id: UUID) -> CandidateBudgetAdmissionContext:
    return CandidateBudgetAdmissionContext(
        run_id=run_id,
        lifecycle_revision=7,
        check_id=uuid4(),
        scope={"subject_ids": [str(uuid4())]},
        scope_fingerprint="b" * 64,
        violated_limits=("max_per_asset_contribution_chunks",),
    )


def test_resume_boundary_returns_same_run_operator_action_for_soft_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    context = _admission_context(run_id)
    orchestrator: Any = _AdmissionOrchestrator(CandidateBudgetOverrideRequired(context))
    state_port: Any = _ResumeState()
    monkeypatch.setattr(resume_module, "coverage_context", lambda *_args: {})

    result = run_resume(
        orchestrator,
        RunResearchCommand(
            run_id=run_id,
            spec={"objective": "issue 305 soft admission"},
            search_plan={"queries": []},
        ),
        state_port=state_port,
    )

    assert result.run_id == run_id
    assert result.final_state == "indexing"
    assert result.outcome == "operator_action_required"
    assert result.error is None
    action = cast(OperatorActionOrchestratorResult, result).operator_action
    assert action is not None
    assert action["kind"] == "candidate_budget_override_required"
    assert action["run_id"] == str(run_id)
    assert action["lifecycle_revision"] == 7
    assert action["check_id"] == str(context.check_id)
    assert action["scope_fingerprint"] == "b" * 64
    assert action["violated_limits"] == ["max_per_asset_contribution_chunks"]
    assert orchestrator.failed_errors == []
    assert smart_cli_disposition(result).exit_code == SMART_RESUMABLE_EXIT


def test_resume_boundary_keeps_hard_gate_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    context = _admission_context(run_id)
    orchestrator: Any = _AdmissionOrchestrator(CandidateBudgetHardRejected(context))
    state_port: Any = _ResumeState()
    monkeypatch.setattr(resume_module, "coverage_context", lambda *_args: {})

    result = run_resume(
        orchestrator,
        RunResearchCommand(
            run_id=run_id,
            spec={"objective": "issue 305 hard admission"},
            search_plan={"queries": []},
        ),
        state_port=state_port,
    )

    assert result.run_id == run_id
    assert result.final_state == "failed"
    assert result.outcome == "failed"
    assert orchestrator.failed_errors
    assert "candidate_budget_hard_rejected" in orchestrator.failed_errors[0]
    assert smart_cli_disposition(result).exit_code == SMART_FAILURE_EXIT
