"""Issue #307 deterministic temporal coverage-gap regressions."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from firecrawl_skill.research_store.smart_result import (
    SMART_RESUMABLE_EXIT,
    OperatorActionOrchestratorResult,
    format_temporal_disposition,
    smart_cli_disposition,
)
from firecrawl_skill.research_store.temporal_coverage import (
    diagnose_temporal_coverage,
    temporal_gap_payload,
)

CLOCK = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def _publication_spec():
    return {
        "time_window": {"start": "2026-08-18", "end": "2026-08-23"},
        "freshness_requirements": [],
    }


def _freshness_spec():
    return {
        "time_window": {"start": None, "end": None},
        "freshness_requirements": [{"max_age_days": 5}],
    }


def _multi_freshness_spec():
    return {
        "time_window": {"start": None, "end": None},
        "freshness_requirements": [
            {"max_age_days": 30},
            {"max_age_days": 5},
        ],
    }


def test_recent_update_cannot_rescue_old_publication_window() -> None:
    diagnostics = diagnose_temporal_coverage(
        [
            {
                "published_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-08-22T00:00:00Z",
                "retrieved_at": CLOCK,
            }
        ],
        _publication_spec(),
        now=CLOCK,
    )
    assert diagnostics.basis == "publication_window"
    assert diagnostics.qualifying_passages == 0
    assert diagnostics.publication_out_of_window == 1


def test_recent_update_does_satisfy_freshness_basis() -> None:
    diagnostics = diagnose_temporal_coverage(
        [
            {
                "published_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-08-22T00:00:00Z",
                "retrieved_at": CLOCK,
            }
        ],
        _freshness_spec(),
        now=CLOCK,
    )
    assert diagnostics.basis == "freshness"
    assert diagnostics.qualifying_passages == 1


def test_strictest_freshness_obligation_drives_stale_diagnostic() -> None:
    diagnostics = diagnose_temporal_coverage(
        [
            {
                "published_at": "2026-08-13T12:00:00Z",
                "retrieved_at": CLOCK,
            }
        ],
        _multi_freshness_spec(),
        now=CLOCK,
    )

    assert diagnostics.qualifying_passages == 0
    assert diagnostics.stale_freshness_authority == 1
    assert diagnostics.future_freshness_authority == 0


def test_retrieval_only_passage_is_typed_missing_publication_authority() -> None:
    diagnostics = diagnose_temporal_coverage(
        [{"retrieved_at": CLOCK}],
        _publication_spec(),
        now=CLOCK,
    )
    assert diagnostics.qualifying_passages == 0
    assert diagnostics.missing_publication_authority == 1
    assert diagnostics.retrieval_only_passages == 1


class TestFutureVersusStaleFreshnessDiagnostics:
    """All-future signal sets must count as future, never as stale.

    The stale test may only fire when at least one non-future authoritative
    timestamp is being evaluated for staleness; an empty non-future set must
    not satisfy ``all(...)`` vacuously.
    """

    def test_only_future_publication_counts_future_not_stale(self) -> None:
        diagnostics = diagnose_temporal_coverage(
            [{"published_at": "2026-09-01T00:00:00Z", "retrieved_at": CLOCK}],
            _freshness_spec(),
            now=CLOCK,
        )
        assert diagnostics.qualifying_passages == 0
        assert diagnostics.future_freshness_authority == 1
        assert diagnostics.stale_freshness_authority == 0

    def test_only_future_update_counts_future_not_stale(self) -> None:
        diagnostics = diagnose_temporal_coverage(
            [{"updated_at": "2026-09-01T00:00:00Z", "retrieved_at": CLOCK}],
            _freshness_spec(),
            now=CLOCK,
        )
        assert diagnostics.qualifying_passages == 0
        assert diagnostics.future_freshness_authority == 1
        assert diagnostics.stale_freshness_authority == 0

    def test_future_publication_plus_future_update_counts_future_once(self) -> None:
        diagnostics = diagnose_temporal_coverage(
            [
                {
                    "published_at": "2026-09-01T00:00:00Z",
                    "updated_at": "2026-09-02T00:00:00Z",
                    "retrieved_at": CLOCK,
                }
            ],
            _freshness_spec(),
            now=CLOCK,
        )
        assert diagnostics.qualifying_passages == 0
        assert diagnostics.future_freshness_authority == 1
        assert diagnostics.stale_freshness_authority == 0

    def test_stale_publication_counts_stale_not_future(self) -> None:
        diagnostics = diagnose_temporal_coverage(
            [{"published_at": "2026-07-01T00:00:00Z", "retrieved_at": CLOCK}],
            _freshness_spec(),
            now=CLOCK,
        )
        assert diagnostics.qualifying_passages == 0
        assert diagnostics.stale_freshness_authority == 1
        assert diagnostics.future_freshness_authority == 0

    def test_stale_update_counts_stale_not_future(self) -> None:
        diagnostics = diagnose_temporal_coverage(
            [{"updated_at": "2026-07-01T00:00:00Z", "retrieved_at": CLOCK}],
            _freshness_spec(),
            now=CLOCK,
        )
        assert diagnostics.qualifying_passages == 0
        assert diagnostics.stale_freshness_authority == 1
        assert diagnostics.future_freshness_authority == 0

    def test_mixed_future_and_stale_counts_both(self) -> None:
        diagnostics = diagnose_temporal_coverage(
            [
                {
                    "published_at": "2026-07-01T00:00:00Z",
                    "updated_at": "2026-09-01T00:00:00Z",
                    "retrieved_at": CLOCK,
                }
            ],
            _freshness_spec(),
            now=CLOCK,
        )
        assert diagnostics.qualifying_passages == 0
        assert diagnostics.stale_freshness_authority == 1
        assert diagnostics.future_freshness_authority == 1


def test_temporal_gap_operator_action_is_resumable_and_bounded() -> None:
    diagnostics = diagnose_temporal_coverage(
        [{"retrieved_at": CLOCK}],
        _publication_spec(),
        now=CLOCK,
    )
    action = temporal_gap_payload(diagnostics, coverage_revision=3)
    result = OperatorActionOrchestratorResult(
        run_id=uuid4(),
        final_state="coverage_review",
        outcome="operator_action_required",
        coverage_revision=3,
        wave_count=4,
        successful_urls=1,
        operator_action=action,
    )

    disposition = smart_cli_disposition(result)
    assert disposition.exit_code == SMART_RESUMABLE_EXIT
    assert (
        disposition.next_action == "resolve_temporal_coverage_gap_then_resume_same_run"
    )
    summary = format_temporal_disposition(result)
    assert summary is not None
    assert "qualifying=0/1" in summary
    assert "missing_publication=1" in summary
    assert action["automatic_scope_relaxation"] is False
    assert action["scope_relaxation_requires"] == "persisted_research_spec_revision"
