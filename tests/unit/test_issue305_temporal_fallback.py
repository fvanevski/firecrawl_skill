from __future__ import annotations

from datetime import datetime, timezone

import pytest

from firecrawl_skill.research_store.fallback_temporal_spec import (
    FallbackTemporalError,
    materialize_smart_fallback_spec,
)

NOW = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)


def _spec(objective: str):
    return materialize_smart_fallback_spec(
        objective,
        execution_mode="autonomous_local",
        evaluated_at=NOW,
    )


@pytest.mark.parametrize(
    "objective",
    [
        "Iran news August 18-23, 2026",
        "Iran news from August 18 to August 23, 2026",
    ],
)
def test_named_month_ranges_materialize_exact_dates(objective: str) -> None:
    spec = _spec(objective)
    assert spec.time_window.start == "2026-08-18"
    assert spec.time_window.end == "2026-08-23"
    assert spec.time_window.uncertainty == "none"


def test_existing_iso_range_is_preserved() -> None:
    spec = _spec("Iran news from 2026-08-18 through 2026-08-23")
    assert spec.time_window.start == "2026-08-18"
    assert spec.time_window.end == "2026-08-23"


def test_existing_relative_clock_remains_timezone_aware_and_deterministic() -> None:
    spec = _spec("Iran news past 5 days")
    assert spec.time_window.end == NOW.isoformat()
    assert spec.time_window.start == datetime(
        2026, 8, 18, 19, 0, tzinfo=timezone.utc
    ).isoformat()


@pytest.mark.parametrize(
    "objective",
    [
        "Iran news last Tuesday",
        "Iran news August 31-32, 2026",
        "Iran news August 23-18, 2026",
    ],
)
def test_ambiguous_impossible_and_reversed_named_temporal_forms_fail_closed(
    objective: str,
) -> None:
    with pytest.raises(FallbackTemporalError) as exc_info:
        _spec(objective)
    message = str(exc_info.value)
    assert "schemas/research-workflow/research-spec-v1.json" in message
    assert "scripts/fsearch_smart --spec-skeleton" in message
