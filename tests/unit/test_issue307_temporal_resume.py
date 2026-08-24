"""Issue #307 resume semantics for recoverable temporal coverage."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from firecrawl_skill.research_store.orchestration import resume as resume_module
from firecrawl_skill.research_store.orchestration.commands import RunResearchCommand
from firecrawl_skill.research_store.orchestration.ports import ResumeCounts
from firecrawl_skill.research_store.orchestration.resume import run_resume
from firecrawl_skill.research_store.orchestrator import OrchestratorResult
from firecrawl_skill.research_store.smart_result import SMART_RESUMABLE_EXIT, smart_cli_disposition


class _State:
    def __init__(self, gap: dict[str, Any] | None, waves: int = 2) -> None:
        self.gap = gap
        self.waves = waves

    def counts(self, _run_id: UUID) -> ResumeCounts:
        return ResumeCounts(waves=self.waves, attempts=2, assets=1)

    def authorized_queries(self, _run_id: UUID) -> list[dict[str, Any]]:
        return []

    def completed_candidates(self, _run_id: UUID) -> set[str]:
        return set()

    def assets(self, _run_id: UUID) -> list[dict[str, Any]]:
        return []

    def packet_revision(self, _run_id: UUID) -> int:
        return 1

    def temporal_coverage_gap(self, _run_id: UUID) -> dict[str, Any] | None:
        return self.gap


class _CoverageReviewOrchestrator:
    def __init__(self) -> None:
        self.orchestrator_config = SimpleNamespace(
            max_adaptive_cycles=2,
            execution_mode="autonomous_local",
        )
        self.executed: list[str] = []

    def _refresh(self, _run_id: UUID) -> tuple[str, int]:
        return "coverage_review", 5

    def _execute_stage(self, stage_name: str, *_args: Any) -> Any:
        self.executed.append(stage_name)
        raise AssertionError("coverage review must not terminalize an exhausted temporal gap")

    def _checkpoint(self, *_args: Any) -> None:
        return None

    def _failed_result(self, run_id: UUID, error: str) -> OrchestratorResult:
        return OrchestratorResult(
            run_id=run_id,
            final_state="failed",
            outcome="failed",
            error=error,
        )


def test_persisted_temporal_gap_at_cycle_limit_returns_operator_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    gap = {
        "kind": "temporal_coverage_gap",
        "status": "unsatisfied",
        "recoverable": True,
        "diagnostics": {
            "basis": "freshness",
            "examined_passages": 2,
            "qualifying_passages": 0,
        },
    }
    state = _State(gap, waves=2)
    orchestrator: Any = _CoverageReviewOrchestrator()
    monkeypatch.setattr(
        resume_module,
        "coverage_context",
        lambda *_args: {"coverage_revision": 3},
    )

    result = run_resume(
        orchestrator,
        RunResearchCommand(
            run_id=run_id,
            spec={"objective": "latest evidence", "freshness_requirements": [{"max_age_days": 5}]},
            search_plan={"queries": []},
            max_adaptive_cycles=2,
        ),
        state_port=state,
    )

    assert result.outcome == "operator_action_required"
    assert result.final_state == "coverage_review"
    assert getattr(result, "operator_action")["kind"] == "temporal_coverage_gap"
    assert orchestrator.executed == []
    assert smart_cli_disposition(result).exit_code == SMART_RESUMABLE_EXIT
