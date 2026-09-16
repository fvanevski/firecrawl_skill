from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID

import pytest

import firecrawl_skill.research_store.orchestration.resume as resume_module
import firecrawl_skill.research_store.research_controller as controller_module
import firecrawl_skill.research_store.research_controller_cli as cli_module
from firecrawl_skill.research_store.orchestration.commands import RunResearchCommand
from firecrawl_skill.research_store.orchestration.resume import run_resume
from firecrawl_skill.research_store.reporting.construction import LocalSynthesisService
from firecrawl_skill.research_store.research_controller import ResearchWorkflowController
from firecrawl_skill.research_store.research_controller_contract import (
    DELIVERY_SELF_SYNTHESIZED,
    DIRECTIVE_SCHEMA_VERSION,
    DISPOSITION_BLOCKED,
    WorkflowDirective,
)
from firecrawl_skill.research_store.run_service import RunStatus
from firecrawl_skill.research_store.stages import StageResult

PUBLIC_ID = "fr_00000000000000000000000000000001"
RUN_ID = UUID("00000000-0000-0000-0000-000000000001")


class _StageRepository:
    def __init__(self, *, attempts: int = 1) -> None:
        self.record: dict[str, Any] = {
            "run_id": RUN_ID,
            "stage_name": "draft",
            "stage_status": "pending",
            "attempts": attempts,
        }

    def get_synthesis_stage(self, run_id: UUID, stage_name: str) -> dict[str, Any]:
        assert run_id == RUN_ID
        assert stage_name == "draft"
        return dict(self.record)

    def update_synthesis_stage(self, record: dict[str, Any]) -> None:
        self.record = dict(record)


class _StageUow:
    def __init__(self, repository: _StageRepository) -> None:
        self.synthesis_stages = repository

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


class _StageUowFactory:
    def __init__(self, repository: _StageRepository) -> None:
        self.repository = repository

    def __call__(self) -> _StageUow:
        return _StageUow(self.repository)


def test_failed_stage_retries_receive_distinct_durable_semantic_identities() -> None:
    repository = _StageRepository()
    uow_factory = _StageUowFactory(repository)
    service: Any = object.__new__(LocalSynthesisService)

    initial_key = service._stage_semantic_idempotency_key(
        uow_factory, RUN_ID, 7, "draft"
    )
    assert initial_key == f"{RUN_ID}-r7-draft"

    service._commit_stage_failure(
        uow_factory,
        RUN_ID,
        "draft",
        "model returned empty content",
    )
    first_retry_key = service._stage_semantic_idempotency_key(
        uow_factory, RUN_ID, 7, "draft"
    )
    assert repository.record["attempts"] == 2
    assert first_retry_key == f"{RUN_ID}-r7-draft-attempt2"

    service._commit_stage_failure(
        uow_factory,
        RUN_ID,
        "draft",
        "model returned empty content",
    )
    second_retry_key = service._stage_semantic_idempotency_key(
        uow_factory, RUN_ID, 7, "draft"
    )
    assert repository.record["attempts"] == 3
    assert second_retry_key == f"{RUN_ID}-r7-draft-attempt3"
    assert len({initial_key, first_retry_key, second_retry_key}) == 3


class _ResumeCounts:
    waves = 2
    attempts = 3
    assets = 4


class _ResumeStatePort:
    @staticmethod
    def counts(_run_id: UUID) -> _ResumeCounts:
        return _ResumeCounts()

    @staticmethod
    def authorized_queries(_run_id: UUID) -> list[dict[str, Any]]:
        return []

    @staticmethod
    def packet_revision(_run_id: UUID) -> int:
        return 7


class _DegradedSynthesisOrchestrator:
    def __init__(self) -> None:
        self.orchestrator_config = SimpleNamespace(
            max_adaptive_cycles=3,
            execution_mode="autonomous_local",
        )
        self.execute_calls = 0

    @staticmethod
    def _refresh(_run_id: UUID) -> tuple[str, int]:
        return "synthesizing", 5

    def _execute_stage(self, stage: str, *_args: Any, **_kwargs: Any) -> StageResult:
        assert stage == "synthesis"
        self.execute_calls += 1
        return StageResult.degraded("synthesis", "draft stage failed")

    @staticmethod
    def _failed_result(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("degraded synthesis must stay resumable")

    @staticmethod
    def _checkpoint(*_args: Any, **_kwargs: Any) -> None:
        return None


def test_resume_returns_after_one_degraded_synthesis_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resume_module, "coverage_context", lambda *_args: {})
    orchestrator = _DegradedSynthesisOrchestrator()
    result = run_resume(
        orchestrator,
        RunResearchCommand(
            run_id=RUN_ID,
            spec={},
            search_plan={},
            max_adaptive_cycles=3,
            context={},
        ),
        state_port=_ResumeStatePort(),
    )

    assert orchestrator.execute_calls == 1
    assert result.outcome == "resumable"
    assert result.final_state == "synthesizing"
    assert result.coverage_revision is None
    assert result.wave_count == 2
    assert result.successful_urls == 4


def _status() -> RunStatus:
    return RunStatus(
        id=RUN_ID,
        external_id=PUBLIC_ID,
        state="synthesizing",
        lifecycle_revision=5,
        reopened_from_revision=None,
        execution_mode="autonomous_local",
        objective="retry a failed synthesis stage",
        declared_outcome=None,
        completed_at=None,
        error=None,
    )


class _RunService:
    @staticmethod
    def status(**_kwargs: Any) -> RunStatus:
        return _status()


class _NoOperatorActions:
    @staticmethod
    def active_for_run(_status: RunStatus) -> None:
        return None

    @staticmethod
    def semantic_fork_child_for_run(_status: RunStatus) -> None:
        return None


def test_controller_translates_runtime_retry_conflict_to_typed_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = SimpleNamespace(
        budget={
            "policy_version": "budget-policy-v1",
            "effective_caps": {
                "max_adaptive_cycles": 3,
                "max_wall_clock_seconds": 60,
            },
        }
    )
    monkeypatch.setattr(controller_module, "load_planning_bundle", lambda *_args: bundle)

    controller: Any = object.__new__(ResearchWorkflowController)
    controller.run_service = _RunService()
    controller.operator_actions = _NoOperatorActions()
    controller.controller_config = SimpleNamespace(
        max_actions=12,
        max_repeated_state=3,
        max_deadline_seconds=300,
    )
    controller._load_policy = lambda _status: SimpleNamespace(
        curated=False,
        delivery_mode=DELIVERY_SELF_SYNTHESIZED,
    )
    controller._reconcile_planning_invocation = lambda *_args, **_kwargs: None
    controller._source_compliance = lambda _status: None
    controller._resume_existing_orchestrator = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(
            ValueError("idempotency key was used for another semantic call")
        )
    )

    directive = controller.continue_run(PUBLIC_ID)

    assert directive.schema_version == DIRECTIVE_SCHEMA_VERSION
    assert directive.run_id == PUBLIC_ID
    assert directive.lifecycle_state == "synthesizing"
    assert directive.lifecycle_revision == 5
    assert directive.disposition == DISPOSITION_BLOCKED
    assert directive.action_kind == "inspect_blocker"
    assert any("idempotency key" in item for item in directive.diagnostics)


def test_cli_emits_typed_runtime_result_without_argparse_usage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _Controller:
        @staticmethod
        def continue_run(run_id: str) -> WorkflowDirective:
            assert run_id == PUBLIC_ID
            return WorkflowDirective(
                schema_version=DIRECTIVE_SCHEMA_VERSION,
                run_id=run_id,
                lifecycle_state="synthesizing",
                lifecycle_revision=5,
                disposition=DISPOSITION_BLOCKED,
                action_kind="inspect_blocker",
                diagnostics=("bounded runtime blocker",),
            )

    monkeypatch.setattr(
        controller_module,
        "build_research_controller",
        lambda: _Controller(),
    )

    exit_code = cli_module.main(["continue", PUBLIC_ID])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 75
    assert captured.err == ""
    assert payload["schema_version"] == DIRECTIVE_SCHEMA_VERSION
    assert payload["run_id"] == PUBLIC_ID
    assert payload["disposition"] == DISPOSITION_BLOCKED


def test_cli_invalid_public_run_id_remains_an_argument_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(["continue", "not-a-public-run-id"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "usage:" in captured.err
    assert "public fr_<uuid>" in captured.err
