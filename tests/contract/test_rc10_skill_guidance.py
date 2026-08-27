"""Keep historical RC-10 evidence separate from the current runtime skill contract."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import UUID

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SKILL_ROOT = SCRIPTS.parent
SKILL_PATH = SKILL_ROOT / "SKILL.md"
WORKFLOW_GUIDE = SKILL_ROOT / "references" / "workflow-state-schema.md"
RUN_ID = "fr_" + "a" * 32

RC10_REFERENCES = {
    "references/release-candidate-gate-rc10.md": (
        "RC-10 Aggregate Release-Candidate Gate",
        "exact resulting `main` SHA",
        "Real release campaign",
        "candidate SHA, dispatch SHA, workflow SHA",
        "artifact ID",
        "artifact digest",
    ),
    "references/release-campaign-timing-diagnostics.md": (
        "release-campaign-timing-v2",
        "PostgreSQL remains authoritative",
        "telemetry_complete",
        "reproducibility_failures",
        "verify_release_campaign_strict.py",
    ),
}


@pytest.mark.parametrize("rel_path,required", RC10_REFERENCES.items())
def test_rc10_historical_reference_exists_and_retains_evidence_contract(
    rel_path: str,
    required: tuple[str, ...],
) -> None:
    content = (SKILL_ROOT / rel_path).read_text(encoding="utf-8")
    for phrase in required:
        assert phrase in content


def test_current_skill_is_controller_runtime_contract_not_release_runbook() -> None:
    content = SKILL_PATH.read_text(encoding="utf-8")
    assert "scripts/fresearch run" in content
    assert "scripts/fresearch continue" in content
    assert "scripts/fresearch result" in content
    assert "references/authoritative-workflows.md" in content
    assert "references/workflow-state-schema.md" in content
    assert "fsearch-smart-checkpoint-handler" not in content
    assert "release-campaign.yml" not in content
    assert "candidate SHA, dispatch SHA" not in content


def test_current_workflow_reference_matches_authoritative_state_machine() -> None:
    from firecrawl_skill.research_store.run_service import (
        PERMITTED_TRANSITIONS,
        TERMINAL_STATES,
    )

    content = WORKFLOW_GUIDE.read_text(encoding="utf-8")
    block = content.split("## State machine", 1)[1].split("```text", 1)[1].split(
        "```", 1
    )[0]
    documented: dict[str, set[str]] = {}
    for line in block.strip().splitlines():
        prior, targets = (part.strip() for part in line.split("→", 1))
        documented[prior] = {part.strip() for part in targets.split("|")}
    expected = {
        prior: set(targets)
        for prior, targets in PERMITTED_TRANSITIONS.items()
        if targets
    }
    assert documented == expected
    for state in TERMINAL_STATES:
        assert f"`{state}`" in content


def test_current_skill_uses_public_controller_actions() -> None:
    from firecrawl_skill.research_store.research_controller_cli import build_parser

    parser = build_parser()
    action_id = "oa_" + "b" * 32
    assert parser.parse_args(["continue", RUN_ID]).command == "continue"
    assert parser.parse_args(["result", RUN_ID]).command == "result"
    assert parser.parse_args(["action", action_id]).command == "action"
    with pytest.raises(SystemExit):
        parser.parse_args(["prepare", RUN_ID])


def test_verify_allows_zero_total_report_without_completion_evidence() -> None:
    from firecrawl_skill.research_store.run_service import ResearchRunService

    run_uuid = UUID(int=1)

    class Runs:
        def list_invocations(self, run_id: UUID) -> list[dict]:
            assert run_id == run_uuid
            return []

    class UnitOfWork:
        runs = Runs()

        def __enter__(self):
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> bool:
            return False

    service = ResearchRunService(lambda: UnitOfWork(), blob_store=object())
    report = service.verify(run_uuid)
    assert report["status"] == "inconclusive"
    assert report["total"] == 0
    assert report["available"] == 0
    assert report["missing"] == 0
    assert report["hash_mismatch"] == 0


def test_trigger_audit_only_schedules_partial_assessment() -> None:
    from firecrawl_skill.research_store.run_service import ResearchRunService

    run_uuid = UUID(int=2)
    captured: dict[str, Any] = {}

    class AuditService:
        def schedule_assessment(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            captured["args"] = args
            captured["kwargs"] = kwargs
            return {
                "assessment_id": "assessment-id",
                "status": kwargs["status"],
                "stages": kwargs["stage_set"],
            }

    service = ResearchRunService(
        lambda: None,
        audit_service_factory=lambda _uow_factory: AuditService(),
    )
    result = service.trigger_audit(
        run_uuid,
        target_hash="a" * 64,
        provider="openai",
        model="audit-model",
        force=True,
        stages=["rubric", "evidence"],
        max_calls=3,
        max_input_tokens=4096,
        fallback_provider="gemini",
        fallback_model="fallback-model",
    )

    assert captured["args"] == (run_uuid,)
    kwargs = captured["kwargs"]
    assert kwargs["status"] == "partial"
    assert kwargs["provider"] == "openai"
    assert kwargs["model"] == "audit-model"
    assert kwargs["stage_set"] == ["rubric", "evidence"]
    for unused_option in (
        "force",
        "max_calls",
        "max_input_tokens",
        "fallback_provider",
        "fallback_model",
    ):
        assert unused_option not in kwargs
    assert result["status"] == "partial"
