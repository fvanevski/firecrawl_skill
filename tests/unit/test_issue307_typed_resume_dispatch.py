"""Issue #307 regressions for exact typed temporal dispatch in smart resume."""

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
from firecrawl_skill.research_store.stages import StageResult
from firecrawl_skill.research_store.temporal_coverage import (
    TemporalCoverageDiagnostics,
    TemporalCoverageUnsatisfied,
)


class _State:
    def __init__(self, *, waves: int) -> None:
        self.waves = waves
        self.chunk_id = uuid4()

    def counts(self, _run_id: UUID) -> ResumeCounts:
        return ResumeCounts(waves=self.waves, attempts=1, assets=1)

    def authorized_queries(self, _run_id: UUID) -> list[dict[str, Any]]:
        return []

    def completed_candidates(self, _run_id: UUID) -> set[str]:
        return set()

    def assets(self, _run_id: UUID) -> list[dict[str, Any]]:
        return [
            {
                "candidate_id": str(uuid4()),
                "chunk_ids": [str(self.chunk_id)],
                "status": "complete",
            }
        ]

    def packet_revision(self, _run_id: UUID) -> int:
        return 1

    def temporal_coverage_gap(self, _run_id: UUID) -> None:
        return None


class _Orchestrator:
    def __init__(self, evidence_outcome: str) -> None:
        self.evidence_outcome = evidence_outcome
        self.state = "indexing"
        self.revision = 5
        self.executed: list[str] = []
        self.orchestrator_config = SimpleNamespace(
            max_adaptive_cycles=2,
            execution_mode="autonomous_local",
        )

    def _refresh(self, _run_id: UUID) -> tuple[str, int]:
        return self.state, self.revision

    def _execute_stage(self, stage_name: str, *_args: Any) -> StageResult:
        self.executed.append(stage_name)
        if stage_name == "indexing":
            self.state = "coverage_review"
            self.revision += 1
            return StageResult.ok("indexing", "indexed")
        if stage_name == "evidence_preparation":
            if self.evidence_outcome == "typed":
                raise TemporalCoverageUnsatisfied(
                    TemporalCoverageDiagnostics(
                        basis="freshness",
                        examined_passages=1,
                        qualifying_passages=0,
                        missing_freshness_authority=1,
                        retrieval_only_passages=1,
                    )
                )
            return StageResult.failed(
                "evidence_preparation",
                "semantic claim extraction failed: malformed structured output",
            )
        raise AssertionError(f"unexpected stage: {stage_name}")

    def _checkpoint(self, *_args: Any) -> None:
        return None

    def _failed_result(self, run_id: UUID, error: str) -> OrchestratorResult:
        return OrchestratorResult(
            run_id=run_id,
            final_state="failed",
            outcome="failed",
            error=error,
        )


def _command(run_id: UUID) -> RunResearchCommand:
    return RunResearchCommand(
        run_id=run_id,
        spec={
            "objective": "latest evidence",
            "time_window": {"start": None, "end": None},
            "freshness_requirements": [{"max_age_days": 5}],
        },
        search_plan={"queries": []},
        max_adaptive_cycles=2,
    )


def test_typed_temporal_exception_dispatches_recoverable_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    state = _State(waves=2)
    orchestrator = _Orchestrator("typed")
    persisted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        resume_module,
        "coverage_context",
        lambda *_args: {"coverage_revision": 3},
    )
    monkeypatch.setattr(
        resume_module,
        "_persist_temporal_gap",
        lambda _orchestrator, _run_id, _revision, gap: persisted.append(gap),
    )

    result = run_resume(orchestrator, _command(run_id), state_port=state)

    assert result.outcome == "operator_action_required"
    assert result.operator_action["kind"] == "temporal_coverage_gap"
    assert result.operator_action["diagnostics"]["missing_freshness_authority"] == 1
    assert result.operator_action["automatic_scope_relaxation"] is False
    assert len(persisted) == 1
    assert orchestrator.executed == ["indexing", "evidence_preparation"]


def test_generic_evidence_error_is_never_reclassified_as_temporal_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    state = _State(waves=2)
    orchestrator = _Orchestrator("generic")
    monkeypatch.setattr(
        resume_module,
        "coverage_context",
        lambda *_args: {"coverage_revision": 3},
    )
    monkeypatch.setattr(
        resume_module,
        "_temporal_gap_from_authority",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("generic evidence errors must not be reclassified")
        ),
    )

    result = run_resume(orchestrator, _command(run_id), state_port=state)

    assert result.outcome == "failed"
    assert result.error == "semantic claim extraction failed: malformed structured output"
    assert orchestrator.executed == ["indexing", "evidence_preparation"]
