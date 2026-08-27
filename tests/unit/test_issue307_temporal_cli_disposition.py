"""Issue #307 temporal/budget actions at the current public controller boundary."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.research_controller import (
    ResearchWorkflowController,
)
from firecrawl_skill.research_store.research_controller_contract import (
    ControllerBlockedError,
    DISPOSITION_OPERATOR,
)

RUN_ID = "fr_" + "b" * 32
ACTION_ID = "oa_" + "c" * 32


class _Actions:
    def __init__(self) -> None:
        self.budget_context: Any = None
        self.scope_payload: dict[str, Any] | None = None

    def ensure_budget_action(self, _status: Any, context: Any) -> Any:
        self.budget_context = context
        return SimpleNamespace(kind="candidate_budget_override", action_id=ACTION_ID)

    def ensure_scope_action(self, _status: Any, payload: dict[str, Any]) -> Any:
        self.scope_payload = dict(payload)
        return SimpleNamespace(kind="scope_change", action_id=ACTION_ID)


def _controller() -> tuple[ResearchWorkflowController, _Actions]:
    controller = ResearchWorkflowController.__new__(ResearchWorkflowController)
    status = SimpleNamespace(
        id=uuid4(),
        external_id=RUN_ID,
        state="coverage_review",
        lifecycle_revision=7,
        error=None,
    )
    controller.run_service = SimpleNamespace(status=lambda **_kwargs: status)
    actions = _Actions()
    controller.operator_actions = actions
    return controller, actions


def test_temporal_gap_becomes_one_durable_public_operator_action() -> None:
    controller, actions = _controller()
    internal_gap = {
        "kind": "temporal_coverage_gap",
        "automatic_scope_relaxation": False,
        "required_resolution": "acquire qualifying authority or fork scope",
        "coverage_revision": 4,
    }
    result = SimpleNamespace(
        outcome="operator_action_required",
        operator_action=internal_gap,
        error=None,
    )

    directive = controller._response_from_orchestrator(RUN_ID, result)

    assert directive.disposition == DISPOSITION_OPERATOR
    assert directive.action_id == ACTION_ID
    assert actions.scope_payload == internal_gap
    public = directive.to_dict()
    assert "coverage_revision" not in public
    assert "required_resolution" not in public


def test_budget_gap_hides_generated_check_parameters_behind_action_id() -> None:
    controller, actions = _controller()
    check_id = uuid4()
    internal_action = {
        "kind": "candidate_budget_override_required",
        "run_id": str(controller.run_service.status().id),
        "lifecycle_revision": 7,
        "check_id": str(check_id),
        "scope": {"subject_ids": [str(uuid4())]},
        "scope_fingerprint": "a" * 64,
        "violated_limits": ["max_generic_page_share"],
    }
    result = SimpleNamespace(
        outcome="operator_action_required",
        operator_action=internal_action,
        error=None,
    )

    directive = controller._response_from_orchestrator(RUN_ID, result)

    assert directive.disposition == DISPOSITION_OPERATOR
    assert directive.action_id == ACTION_ID
    assert actions.budget_context.check_id == check_id
    public = directive.to_dict()
    for internal in ("check_id", "scope_fingerprint", "violated_limits"):
        assert internal not in public


def test_unknown_internal_operator_action_fails_closed() -> None:
    controller, _actions = _controller()
    result = SimpleNamespace(
        outcome="operator_action_required",
        operator_action={"kind": "future_operator_gate"},
        error=None,
    )
    with pytest.raises(ControllerBlockedError, match="unsupported orchestrator operator action"):
        controller._response_from_orchestrator(RUN_ID, result)
