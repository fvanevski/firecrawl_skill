from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from firecrawl_skill.research_store.research_controller import (
    ResearchWorkflowController,
)
from firecrawl_skill.research_store.research_controller_contract import (
    DIRECTIVE_SCHEMA_VERSION,
    RESULT_SCHEMA_VERSION,
    DISPOSITION_CANCELLED,
    DISPOSITION_COMPLETED,
    DISPOSITION_FAILED,
    DISPOSITION_PARTIAL,
    ControllerBoundError,
    ControllerConfig,
    ProgressGuard,
    ResearchResult,
    WorkflowDirective,
    terminal_disposition,
    validate_public_run_id,
)
from firecrawl_skill.research_store.run_service import RunStatus

PUBLIC_ID = "fr_00000000000000000000000000000001"


def _status(state: str, revision: int = 3) -> RunStatus:
    return RunStatus(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        external_id=PUBLIC_ID,
        state=state,
        lifecycle_revision=revision,
        reopened_from_revision=None,
        execution_mode="deterministic_debug",
        objective="controller contract",
        declared_outcome=None,
        completed_at=None,
        error=None,
    )


@pytest.mark.parametrize(
    ("state", "disposition"),
    [
        ("completed", DISPOSITION_COMPLETED),
        ("partial", DISPOSITION_PARTIAL),
        ("failed", DISPOSITION_FAILED),
        ("cancelled", DISPOSITION_CANCELLED),
    ],
)
def test_terminal_disposition_is_typed(state: str, disposition: str) -> None:
    assert terminal_disposition(state) == disposition


@pytest.mark.parametrize(
    "value",
    [
        "00000000-0000-0000-0000-000000000001",
        "fr_1",
        "fc_00000000000000000000000000000001",
        "fr_not-a-uuid",
    ],
)
def test_public_controller_boundary_rejects_internal_identity(value: str) -> None:
    with pytest.raises(ValueError, match="public fr_<uuid>"):
        validate_public_run_id(value)


def test_public_controller_boundary_accepts_fr_identity() -> None:
    assert validate_public_run_id(PUBLIC_ID) == PUBLIC_ID


def test_progress_guard_fails_closed_on_repeated_persisted_state() -> None:
    guard = ProgressGuard(
        ControllerConfig(
            max_actions=10,
            max_repeated_state=2,
            max_deadline_seconds=60,
        )
    )
    status = _status("retrieving")
    guard.observe(status)
    guard.observe(status)
    with pytest.raises(ControllerBoundError, match="without progress"):
        guard.observe(status)


def test_progress_guard_respects_authoritative_deadline_cap() -> None:
    guard = ProgressGuard(ControllerConfig(max_deadline_seconds=10))
    guard.tighten_deadline(4)
    assert guard.deadline_seconds == 4
    guard.tighten_deadline(8)
    assert guard.deadline_seconds == 4


def test_directive_contains_only_public_control_identity() -> None:
    directive = WorkflowDirective(
        schema_version=DIRECTIVE_SCHEMA_VERSION,
        run_id=PUBLIC_ID,
        lifecycle_state="coverage_review",
        lifecycle_revision=7,
        disposition="continue_automatic",
        action_kind="continue",
    ).to_dict()
    assert directive["run_id"] == PUBLIC_ID
    assert directive["result_ready"] is False
    assert "research_spec_id" not in directive
    assert "run_uuid" not in directive
    assert "candidate_budget_check_id" not in directive


def test_partial_result_is_terminal_but_not_objective_satisfied() -> None:
    result = ResearchResult(
        schema_version=RESULT_SCHEMA_VERSION,
        run_id=PUBLIC_ID,
        objective="controller contract",
        lifecycle_state="partial",
        lifecycle_revision=8,
        disposition=DISPOSITION_PARTIAL,
        terminal=True,
        outcome="partial",
        result_ready=True,
        handoff_ready=True,
        objective_satisfied=False,
        limitations=(
            "terminal partial result does not establish objective satisfaction",
        ),
    ).to_dict()
    assert result["terminal"] is True
    assert result["result_ready"] is True
    assert result["objective_satisfied"] is False


@pytest.mark.parametrize(
    ("internal_kind", "public_kind"),
    [
        ("candidate_budget_override_required", "candidate_budget_authorization"),
        ("temporal_coverage_gap", "temporal_scope_decision"),
        ("unexpected_internal_detail", "operator_review"),
    ],
)
def test_operator_actions_hide_generated_internal_parameters(
    internal_kind: str,
    public_kind: str,
) -> None:
    action = {
        "kind": internal_kind,
        "check_id": "internal-check-id",
        "violated_limits": ["internal-limit"],
    }
    assert (
        ResearchWorkflowController._public_operator_action_kind(action)
        == public_kind
    )


class _OperatorRuns:
    def list_events(
        self,
        *_args: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        if kwargs.get("event_type") == "controller.operator_action_observed":
            return [
                {
                    "payload": {
                        "schema_version": "controller-operator-action-v1",
                        "action_kind": "candidate_budget_authorization",
                        "lifecycle_revision": 3,
                    }
                }
            ]
        return []


class _NoPackets:
    @staticmethod
    def get_evidence_packet(_run_id):
        return None


class _OperatorUow:
    def __init__(self) -> None:
        self.runs = _OperatorRuns()
        self.evidence_packets = _NoPackets()

    def __enter__(self) -> "_OperatorUow":
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False


class _OperatorRunService:
    @staticmethod
    def status(**_kwargs: Any) -> RunStatus:
        return _status("coverage_review", 3)

    @staticmethod
    def uow_factory() -> _OperatorUow:
        return _OperatorUow()


def test_status_preserves_active_human_authorization_boundary() -> None:
    controller: Any = object.__new__(ResearchWorkflowController)
    controller.run_service = _OperatorRunService()

    directive = controller.status(PUBLIC_ID)

    assert directive.disposition == "operator_action_required"
    assert directive.action_kind == "candidate_budget_authorization"
    assert directive.result_ready is False
