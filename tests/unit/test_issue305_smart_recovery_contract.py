from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from firecrawl_skill.research_store.candidate_budget_outcomes import (
    CandidateBudgetHardRejected,
    CandidateBudgetOverrideRequired,
    classify_persisted_completion_admission,
)
from firecrawl_skill.research_store.orchestrator import OrchestratorResult
from firecrawl_skill.research_store.smart_result import (
    SMART_FAILURE_EXIT,
    SMART_RESUMABLE_EXIT,
    SMART_SUCCESS_EXIT,
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
