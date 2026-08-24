"""Issue #305 fallback regressions as superseded by issue #307 semantics."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from firecrawl_skill.research_domain import load_model, serialize_model
from firecrawl_skill.research_domain.models import ResearchSpec
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


def test_spec_skeleton_ignores_ambient_run_and_round_trips_domain_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRECRAWL_RESEARCH_RUN_ID", "fr_ambient_should_not_bind")
    result = subprocess.run(
        [
            sys.executable,
            str(FSEARCH_SMART),
            "Iran news August 18-23, 2026",
            "--spec-skeleton",
        ],
        env=os.environ.copy(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    loaded = load_model(payload)
    assert isinstance(loaded, ResearchSpec)
    assert serialize_model(loaded) == payload
    assert loaded.time_window.start == "2026-08-18"
    assert loaded.time_window.end == "2026-08-23"


def test_spec_skeleton_rejects_explicit_run_binding() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(FSEARCH_SMART),
            "Iran news August 18-23, 2026",
            "--spec-skeleton",
            "--research-run-id",
            "fr_explicit",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "--spec-skeleton is standalone" in result.stderr
