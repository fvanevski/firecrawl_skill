"""Focused regressions for #310 controller-policy directive authority."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Self
from uuid import UUID

import pytest

from firecrawl_skill.research_store.research_controller import (
    ResearchWorkflowController,
)
from firecrawl_skill.research_store.research_controller_contract import (
    DISPOSITION_BLOCKED,
    ControllerBlockedError,
    ControllerConfig,
    WorkflowDirective,
)
from firecrawl_skill.research_store.run_service import RunStatus

PUBLIC_ID = "fr_00000000000000000000000000000001"
RUN_ID = UUID("00000000-0000-0000-0000-000000000001")


def _status() -> RunStatus:
    return RunStatus(
        id=RUN_ID,
        external_id=PUBLIC_ID,
        state="created",
        lifecycle_revision=0,
        reopened_from_revision=None,
        execution_mode="deterministic_debug",
        objective="controller policy authority",
        declared_outcome=None,
        completed_at=None,
        error=None,
    )


class _EvidencePackets:
    @staticmethod
    def get_evidence_packet(_run_id: UUID) -> None:
        return None


class _Runs:
    def __init__(self, policy_payload: dict[str, Any] | None) -> None:
        self.policy_payload = policy_payload

    def list_events(
        self,
        _run_id: UUID,
        *,
        event_type: str,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        assert limit >= 1
        assert offset == 0
        if event_type != "controller.policy_recorded" or self.policy_payload is None:
            return []
        return [{"payload": self.policy_payload}]


class _Uow:
    def __init__(self, policy_payload: dict[str, Any] | None) -> None:
        self.runs = _Runs(policy_payload)
        self.evidence_packets = _EvidencePackets()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


class _RunService:
    def __init__(self, policy_payload: dict[str, Any] | None) -> None:
        self.policy_payload = policy_payload

    @staticmethod
    def status(**_kwargs: Any) -> RunStatus:
        return _status()

    def uow_factory(self) -> _Uow:
        return _Uow(self.policy_payload)


def _controller(policy_payload: dict[str, Any] | None) -> ResearchWorkflowController:
    controller: Any = object.__new__(ResearchWorkflowController)
    controller.run_service = _RunService(policy_payload)
    controller.controller_config = ControllerConfig()
    return controller


def _valid_policy(**updates: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "research-controller-policy-v2",
        "retained_only": False,
        "curated": False,
        "evaluated_at": datetime(2026, 8, 24, tzinfo=timezone.utc).isoformat(),
    }
    payload.update(updates)
    return payload


def test_status_and_continue_agree_when_controller_policy_is_missing() -> None:
    controller = _controller(None)

    status_directive = controller.status(PUBLIC_ID)
    continue_directive = controller.continue_run(PUBLIC_ID)

    assert isinstance(continue_directive, WorkflowDirective)
    for directive in (status_directive, continue_directive):
        assert directive.disposition == DISPOSITION_BLOCKED
        assert directive.action_kind == "inspect_blocker"
        assert directive.lifecycle_state == "created"
        assert directive.lifecycle_revision == 0
        assert directive.result_ready is False
        assert any(
            "no canonical controller policy" in item for item in directive.diagnostics
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": "research-controller-policy-v2",
            "evaluated_at": datetime(2026, 8, 24, tzinfo=timezone.utc).isoformat(),
        },
        _valid_policy(retained_only="false"),
        _valid_policy(retained_only=0),
        _valid_policy(retained_only=None),
    ],
)
def test_load_policy_rejects_missing_or_non_boolean_retained_only(
    payload: dict[str, Any],
) -> None:
    controller = _controller(payload)

    with pytest.raises(
        ControllerBlockedError,
        match="retained-only policy is malformed",
    ):
        controller._load_policy(_status())


def test_status_fails_closed_for_malformed_retained_only_policy() -> None:
    controller = _controller(_valid_policy(retained_only="false"))

    directive = controller.status(PUBLIC_ID)

    assert directive.disposition == DISPOSITION_BLOCKED
    assert directive.action_kind == "inspect_blocker"
    assert any(
        "retained-only policy is malformed" in item for item in directive.diagnostics
    )


@pytest.mark.parametrize("curated", ["false", 0, None])
def test_load_policy_rejects_non_boolean_curated_mode(curated: Any) -> None:
    controller = _controller(_valid_policy(curated=curated))

    with pytest.raises(
        ControllerBlockedError,
        match="curated policy is malformed",
    ):
        controller._load_policy(_status())


def test_load_policy_rejects_previous_controller_policy_schema() -> None:
    controller = _controller(
        _valid_policy(schema_version="research-controller-policy-v1")
    )

    with pytest.raises(ControllerBlockedError, match="controller policy is malformed"):
        controller._load_policy(_status())
