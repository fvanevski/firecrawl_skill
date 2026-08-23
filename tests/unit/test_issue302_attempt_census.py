"""Issue #302 regressions for durable smart-search attempt census."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Self
from uuid import UUID

from firecrawl_skill.research_store.orchestrator import OrchestratorResult
from firecrawl_skill.research_store.resume_state_repository import (
    PostgresResumeStateReader,
)
from firecrawl_skill.research_store.smart_result import (
    SmartOrchestratorResult,
    format_attempt_census,
)

RUN_ID = UUID(int=302)
TIME = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)


class _Attempts:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def count_for_run(self, run_id: UUID) -> int:
        assert run_id == RUN_ID
        return len(self.rows)

    def list_attempts_for_run(
        self,
        run_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        assert run_id == RUN_ID
        return [dict(row) for row in self.rows[offset : offset + limit]]


class _Candidates:
    def get_candidate(
        self,
        candidate_id: str,
        *,
        run_id: UUID,
    ) -> dict[str, Any]:
        assert run_id == RUN_ID
        return {
            "id": candidate_id,
            "canonical_url": f"https://example.test/{candidate_id}",
            "original_url": f"https://origin.test/{candidate_id}",
        }


class _Uow:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.extraction_attempts = _Attempts(rows)
        self.candidates = _Candidates()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _rows() -> list[dict[str, Any]]:
    result = []
    for index in range(5):
        result.append(
            {
                "id": f"success-{index}",
                "candidate_id": f"candidate-{index}",
                "start_time": TIME,
                "exit_status": "succeeded",
                "failure_class": "none",
            }
        )
    result.append(
        {
            "id": "53255b9f-25f5-43a4-ba77-ed3e9a26f092",
            "candidate_id": "timed-out-candidate",
            "start_time": TIME,
            "exit_status": "cancelled",
            "failure_class": "timeout",
        }
    )
    return result


def test_attempt_census_surfaces_five_of_six_and_timeout_identity() -> None:
    rows = _rows()
    reader = PostgresResumeStateReader(lambda: _Uow(rows))

    census = reader.attempt_census(RUN_ID)

    assert census.attempted == 6
    assert census.succeeded == 5
    assert census.unsuccessful == 1
    assert census.failure_counts == {"timeout": 1}
    assert census.unsuccessful_attempts == (
        {
            "attempt_id": "53255b9f-25f5-43a4-ba77-ed3e9a26f092",
            "candidate_id": "timed-out-candidate",
            "target_url": "https://example.test/timed-out-candidate",
            "exit_status": "cancelled",
            "failure_class": "timeout",
        },
    )


def test_repeated_resume_census_is_stable_and_not_double_counted() -> None:
    rows = _rows()
    reader = PostgresResumeStateReader(lambda: _Uow(rows))

    first = reader.attempt_census(RUN_ID)
    second = reader.attempt_census(RUN_ID)

    assert first == second
    assert second.attempted == 6


def test_smart_result_preserves_successful_urls_and_formats_census() -> None:
    rows = _rows()
    census = PostgresResumeStateReader(lambda: _Uow(rows)).attempt_census(RUN_ID)
    base = OrchestratorResult(
        run_id=RUN_ID,
        final_state="partial",
        outcome="partial",
        wave_count=2,
        successful_urls=5,
    )

    result = SmartOrchestratorResult.from_result(base, census)

    assert result.successful_urls == 5
    assert format_attempt_census(result) == (
        "Attempt census: 5 of 6 succeeded; 1 unsuccessful; timeout=1"
    )
    assert result.to_dict()["failure_counts"] == {"timeout": 1}
