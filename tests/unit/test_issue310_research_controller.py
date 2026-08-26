from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID

import pytest

from firecrawl_skill.research_domain.models import MechanicalStatus, ResearchQuestion
from firecrawl_skill.research_store.budget_policy import conservative_research_spec
from firecrawl_skill.research_store.research_controller import (
    ResearchWorkflowController,
)
from firecrawl_skill.research_store.research_controller_contract import (
    DIRECTIVE_SCHEMA_VERSION,
    DISPOSITION_BLOCKED,
    DISPOSITION_CANCELLED,
    DISPOSITION_COMPLETED,
    DISPOSITION_FAILED,
    DISPOSITION_PARTIAL,
    RESULT_SCHEMA_VERSION,
    ControllerBlockedError,
    ControllerBoundError,
    ControllerConfig,
    ProgressGuard,
    ResearchResult,
    WorkflowDirective,
    terminal_disposition,
    validate_public_run_id,
)
from firecrawl_skill.research_store.retained_review_service import RetainedReviewService
from firecrawl_skill.research_store.run_service import RunStatus
from firecrawl_skill.research_store.smart_search_application import canonical_plan

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


def test_planning_invocation_identity_is_restart_stable_and_external() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000001")
    first = ResearchWorkflowController._planning_external_invocation_id(run_id)
    second = ResearchWorkflowController._planning_external_invocation_id(run_id)
    assert first == second
    assert first.startswith("fc_")
    assert len(first) == 35


def test_persisted_query_identity_is_run_scoped_and_restart_stable() -> None:
    spec = conservative_research_spec("same objective can be rerun", "general")
    queries = [{"query": spec.objective, "facet": "objective"}]
    first_run = UUID("00000000-0000-0000-0000-000000000101")
    second_run = UUID("00000000-0000-0000-0000-000000000102")

    first = canonical_plan(spec, queries, run_id=first_run)
    replay = canonical_plan(spec, queries, run_id=first_run)
    second = canonical_plan(spec, queries, run_id=second_run)

    assert first["queries"][0]["query_id"] == replay["queries"][0]["query_id"]
    assert first["queries"][0]["query_id"] != second["queries"][0]["query_id"]


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


def test_operator_directive_exposes_only_public_action_identity() -> None:
    directive = WorkflowDirective(
        schema_version=DIRECTIVE_SCHEMA_VERSION,
        run_id=PUBLIC_ID,
        lifecycle_state="coverage_review",
        lifecycle_revision=3,
        disposition="operator_action_required",
        action_kind="candidate_budget_authorization",
        action_id="oa_00000000000000000000000000000001",
    ).to_dict()
    assert directive["action_id"].startswith("oa_")
    for forbidden in (
        "check_id",
        "violated_limits",
        "scope_fingerprint",
        "research_spec_id",
    ):
        assert forbidden not in directive


class _RetainedCorpus:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def search_assets(
        self,
        query: str,
        *,
        candidate_limit: int,
        **_kwargs: Any,
    ) -> tuple[Any, list[dict[str, str]]]:
        self.calls.append((query, candidate_limit))
        candidates = {
            "first retained facet": [
                {
                    "candidate_id": "00000000-0000-0000-0000-000000000011",
                    "snapshot_id": "00000000-0000-0000-0000-000000000111",
                    "url": "https://example.test/first-a",
                },
                {
                    "candidate_id": "00000000-0000-0000-0000-000000000012",
                    "snapshot_id": "00000000-0000-0000-0000-000000000112",
                    "url": "https://example.test/first-b",
                },
            ],
            "second retained facet": [
                {
                    "candidate_id": "00000000-0000-0000-0000-000000000021",
                    "snapshot_id": "00000000-0000-0000-0000-000000000121",
                    "url": "https://example.test/second",
                }
            ],
        }[query]
        execution = SimpleNamespace(mechanical_status=MechanicalStatus.SUCCEEDED)
        return execution, candidates[:candidate_limit]


class _RetainedSnapshots:
    @staticmethod
    def link_run_asset(*_args: Any, **_kwargs: Any) -> None:
        return None


class _RetainedRuns:
    @staticmethod
    def append_event(*_args: Any, **_kwargs: Any) -> None:
        return None


class _RetainedUow:
    def __init__(self) -> None:
        self.snapshots = _RetainedSnapshots()
        self.runs = _RetainedRuns()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    @staticmethod
    def commit() -> None:
        return None


class _RetainedRunService:
    @staticmethod
    def uow_factory() -> _RetainedUow:
        return _RetainedUow()


def _two_question_spec():
    spec = conservative_research_spec("retained allocation", "general")
    return replace(
        spec,
        questions=(
            ResearchQuestion(
                UUID("00000000-0000-0000-0000-000000000201"),
                "first retained facet",
            ),
            ResearchQuestion(
                UUID("00000000-0000-0000-0000-000000000202"),
                "second retained facet",
            ),
        ),
    )


def _retained_service(corpus: _RetainedCorpus, *, limit: int) -> Any:
    service: Any = object.__new__(RetainedReviewService)
    service.corpus_service = corpus
    service.run_service = _RetainedRunService()
    service.controller_config = ControllerConfig(max_retained_candidates=limit)
    return service


def test_retained_selection_allocates_global_cap_across_all_queries() -> None:
    corpus = _RetainedCorpus()
    service = _retained_service(corpus, limit=2)
    bundle: Any = SimpleNamespace(
        budget={"effective_caps": {"max_retrieval_candidates": 2}},
        spec=_two_question_spec(),
        spec_revision=1,
    )

    selection = service._select(_status("retrieving"), bundle)

    assert corpus.calls == [
        ("first retained facet", 1),
        ("second retained facet", 1),
    ]
    assert len(selection) == 2
    assert [item["query_index"] for item in selection] == ["0", "1"]
    assert selection[1]["chunk_id"] == "00000000-0000-0000-0000-000000000021"


def test_retained_selection_fails_closed_when_query_count_exceeds_cap() -> None:
    corpus = _RetainedCorpus()
    service = _retained_service(corpus, limit=1)
    bundle: Any = SimpleNamespace(
        budget={"effective_caps": {"max_retrieval_candidates": 1}},
        spec=_two_question_spec(),
        spec_revision=1,
    )

    with pytest.raises(ControllerBlockedError, match="retained query scope exceeds"):
        service._select(_status("retrieving"), bundle)

    assert corpus.calls == []


class _OperatorRuns:
    def list_events(
        self,
        *_args: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        event_type = kwargs.get("event_type")
        if event_type == "controller.policy_recorded":
            return [
                {
                    "payload": {
                        "schema_version": "research-controller-policy-v2",
                        "retained_only": False,
                        "curated": False,
                        "evaluated_at": "2026-08-24T21:00:00+00:00",
                    }
                }
            ]
        return []


class _MissingPolicyRuns:
    @staticmethod
    def list_events(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []


class _NoPackets:
    @staticmethod
    def get_evidence_packet(_run_id):
        return None


class _OperatorUow:
    def __init__(self) -> None:
        self.runs = _OperatorRuns()
        self.evidence_packets = _NoPackets()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


class _MissingPolicyUow:
    def __init__(self) -> None:
        self.runs = _MissingPolicyRuns()
        self.evidence_packets = _NoPackets()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


class _OperatorActions:
    @staticmethod
    def active_for_run(_status: RunStatus) -> Any:
        return SimpleNamespace(
            kind="candidate_budget_authorization",
            action_id="oa_00000000000000000000000000000001",
        )


class _OperatorRunService:
    @staticmethod
    def status(**_kwargs: Any) -> RunStatus:
        return _status("coverage_review", 3)

    @staticmethod
    def uow_factory() -> _OperatorUow:
        return _OperatorUow()


class _MissingPolicyRunService:
    @staticmethod
    def status(**_kwargs: Any) -> RunStatus:
        return _status("created", 0)

    @staticmethod
    def uow_factory() -> _MissingPolicyUow:
        return _MissingPolicyUow()


def test_status_preserves_active_human_authorization_boundary() -> None:
    controller: Any = object.__new__(ResearchWorkflowController)
    controller.run_service = _OperatorRunService()
    controller.operator_actions = _OperatorActions()

    directive = controller.status(PUBLIC_ID)

    assert directive.disposition == "operator_action_required"
    assert directive.action_kind == "candidate_budget_authorization"
    assert directive.action_id == "oa_00000000000000000000000000000001"
    assert directive.result_ready is False


def test_continue_missing_controller_policy_returns_blocked_directive() -> None:
    controller: Any = object.__new__(ResearchWorkflowController)
    controller.run_service = _MissingPolicyRunService()
    controller.controller_config = ControllerConfig()

    directive = controller.continue_run(PUBLIC_ID)

    assert directive.disposition == DISPOSITION_BLOCKED
    assert directive.action_kind == "inspect_blocker"
    assert directive.lifecycle_state == "created"
    assert directive.lifecycle_revision == 0
    assert directive.result_ready is False
    assert any(
        "no canonical controller policy" in item for item in directive.diagnostics
    )
