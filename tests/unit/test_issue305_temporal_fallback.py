"""Issue #305 fallback regressions as superseded by issue #307/#311 semantics."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from firecrawl_skill.research_store.fallback_temporal_spec import (
    FallbackTemporalError,
    materialize_smart_fallback_spec,
)

NOW = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]
FSEARCH_SMART = ROOT / "scripts" / "fsearch_smart"


def _spec(objective: str):
    return materialize_smart_fallback_spec(
        objective,
        execution_mode="autonomous_local",
        evaluated_at=NOW,
    )


def test_compact_named_month_range_materializes_exact_dates() -> None:
    spec = _spec("Iran news August 18-23, 2026")
    assert spec.time_window.start == "2026-08-18"
    assert spec.time_window.end == "2026-08-23"
    assert spec.time_window.uncertainty == "none"


def test_existing_iso_range_is_preserved() -> None:
    spec = _spec("Iran news from 2026-08-18 through 2026-08-23")
    assert spec.time_window.start == "2026-08-18"
    assert spec.time_window.end == "2026-08-23"


def test_relative_past_days_is_freshness_only_under_issue307() -> None:
    spec = _spec("Iran news past 5 days")
    assert spec.time_window.start is None
    assert spec.time_window.end is None
    assert spec.freshness_requirements[0].max_age_days == 5


@pytest.mark.parametrize(
    "objective",
    [
        "Iran news last Tuesday",
        "Iran news August 31-32, 2026",
        "Iran news August 23-18, 2026",
        "Iran news from August 18 to August 23, 2026",
    ],
)
def test_unsupported_named_temporal_forms_fail_closed(objective: str) -> None:
    with pytest.raises(FallbackTemporalError) as exc_info:
        _spec(objective)
    message = str(exc_info.value)
    assert "schemas/research-workflow/research-spec-v1.json" in message
    assert "or use the normal semantic smart-objective interpreter" in message


def test_deprecated_public_alias_does_not_expose_spec_skeleton() -> None:
    source = FSEARCH_SMART.read_text(encoding="utf-8")
    assert "spec_skeleton" not in source
    assert "--spec-skeleton" not in source


def test_deprecated_public_alias_does_not_accept_internal_run_binding() -> None:
    source = FSEARCH_SMART.read_text(encoding="utf-8")
    assert "--research-run-id" not in source
    assert 'with_name("fresearch")' in source
    assert '"run", *args' in source
