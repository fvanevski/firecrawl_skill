"""Issue #311 current-coverage authority for deterministic candidate scoring."""

from __future__ import annotations

from uuid import uuid4

from firecrawl_skill.research_store.acquisition.temporal_acquisition import (
    TemporalAcquisitionService,
)
from firecrawl_skill.research_store.budget_policy import conservative_research_spec


class _Coverage:
    def __init__(self, current_revision: int, snapshot: dict | None) -> None:
        self.current_revision = current_revision
        self.snapshot = snapshot

    def get_current_revision(self, run_id):
        del run_id
        return self.current_revision

    def get_latest_snapshot(self, run_id):
        del run_id
        return self.snapshot


class _Uow:
    def __init__(self, coverage) -> None:
        self.coverage = coverage


def _snapshot(question_id: str, *, revision: int, status: str) -> dict:
    return {
        "coverage_revision": revision,
        "ledger": {
            "schema_version": "coverage-ledger-v1",
            "items": [
                {
                    "item_type": "question",
                    "subject_id": question_id,
                    "status": status,
                }
            ],
        },
    }


def test_stale_snapshot_cannot_close_current_question_gap() -> None:
    spec = conservative_research_spec("current coverage authority", "general")
    question_id = str(spec.questions[0].question_id)
    uow = _Uow(
        _Coverage(
            current_revision=2,
            snapshot=_snapshot(question_id, revision=1, status="satisfied"),
        )
    )

    gaps = TemporalAcquisitionService._coverage_gap_question_ids(
        uow,
        uuid4(),
        spec,
    )

    assert gaps == (question_id,)


def test_current_snapshot_may_close_satisfied_question_gap() -> None:
    spec = conservative_research_spec("current coverage authority", "general")
    question_id = str(spec.questions[0].question_id)
    uow = _Uow(
        _Coverage(
            current_revision=2,
            snapshot=_snapshot(question_id, revision=2, status="satisfied"),
        )
    )

    gaps = TemporalAcquisitionService._coverage_gap_question_ids(
        uow,
        uuid4(),
        spec,
    )

    assert gaps == ()


def test_missing_snapshot_or_repository_conservatively_keeps_questions_open() -> None:
    spec = conservative_research_spec("current coverage authority", "general")
    question_id = str(spec.questions[0].question_id)

    assert TemporalAcquisitionService._coverage_gap_question_ids(
        _Uow(_Coverage(current_revision=3, snapshot=None)),
        uuid4(),
        spec,
    ) == (question_id,)

    class _NoCoverage:
        pass

    assert TemporalAcquisitionService._coverage_gap_question_ids(
        _NoCoverage(),
        uuid4(),
        spec,
    ) == (question_id,)
