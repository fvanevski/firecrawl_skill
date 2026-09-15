"""Issue #388 regressions for packet-bound terminal coverage authority."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from firecrawl_skill.research_store.research_controller import (
    ControllerPolicy,
    ResearchWorkflowController,
)
from firecrawl_skill.research_store.research_controller_contract import (
    ControllerBlockedError,
    DISPOSITION_BLOCKED,
)


def _completed_controller_with_blocked_handoff():
    external_id = f"fr_{uuid4().hex}"
    status = SimpleNamespace(
        id=uuid4(),
        external_id=external_id,
        state="completed",
        objective="issue 388 packet-bound coverage authority",
        lifecycle_revision=7,
        declared_outcome="satisfied",
        error=None,
    )
    run_service = MagicMock()
    run_service.status.return_value = status
    controller = ResearchWorkflowController(
        config=MagicMock(),
        run_service=run_service,
        invocation_service=MagicMock(),
        corpus_service=MagicMock(),
        coverage_service=MagicMock(),
        evidence_service=MagicMock(),
        semantic_service=MagicMock(),
        orchestrator_factory=MagicMock(),
    )
    controller._load_policy = MagicMock(  # type: ignore[method-assign]
        return_value=ControllerPolicy(
            retained_only=False,
            evaluated_at=datetime.now(timezone.utc),
            curated=False,
            delivery_mode="host_handoff",
        )
    )
    controller._build_public_handoff = MagicMock(  # type: ignore[method-assign]
        side_effect=ControllerBlockedError(
            "completed lifecycle is not backed by a sufficient "
            "EvidencePacket-bound coverage snapshot"
        )
    )
    controller._source_compliance = MagicMock(  # type: ignore[method-assign]
        return_value=None
    )
    return controller, external_id


def test_completed_status_does_not_claim_objective_satisfied_when_handoff_is_blocked():
    controller, external_id = _completed_controller_with_blocked_handoff()

    directive = controller.status(external_id)

    assert directive.lifecycle_state == "completed"
    assert directive.disposition == DISPOSITION_BLOCKED
    assert directive.result_ready is False
    assert directive.handoff_ready is False
    assert directive.objective_satisfied is False


def test_completed_result_does_not_claim_objective_satisfied_when_handoff_is_blocked():
    controller, external_id = _completed_controller_with_blocked_handoff()

    result = controller.result(external_id)

    assert result.lifecycle_state == "completed"
    assert result.disposition == DISPOSITION_BLOCKED
    assert result.result_ready is False
    assert result.handoff_ready is False
    assert result.objective_satisfied is False
    assert result.handoff is None
    assert any(
        "EvidencePacket-bound coverage snapshot" in message
        for message in result.diagnostics
    )
