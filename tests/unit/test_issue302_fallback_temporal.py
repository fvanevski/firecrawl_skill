"""Issue #302 regressions for smart-search temporal fallback authority."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from firecrawl_skill.research_domain import serialize_model
from firecrawl_skill.research_domain.models import ExecutionMode
from firecrawl_skill.research_store.fallback_temporal_spec import (
    FallbackTemporalError,
    materialize_smart_fallback_spec,
)
from firecrawl_skill.research_store.plan_recency import (
    TemporalPlanTransportError,
    plan_query_recency_tbs,
)
from firecrawl_skill.research_store.recency import normalize_recency_window
from firecrawl_skill.research_store.temporal_policy import (
    passage_temporally_qualifies,
)

CLOCK = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def test_explicit_iso_range_is_materialized_and_mode_aligned() -> None:
    spec = materialize_smart_fallback_spec(
        "Review changes from 2026-08-17 through 2026-08-22",
        execution_mode=ExecutionMode.AUTONOMOUS_LOCAL,
        evaluated_at=CLOCK,
    )

    assert spec.execution_mode is ExecutionMode.AUTONOMOUS_LOCAL
    assert spec.time_window.start == "2026-08-17"
    assert spec.time_window.end == "2026-08-22"
    assert spec.time_window.uncertainty == "none"
    assert len(spec.freshness_requirements) == 1
    assert spec.freshness_requirements[0].max_age_days is None


def test_past_five_days_uses_one_explicit_clock() -> None:
    spec = materialize_smart_fallback_spec(
        "Summarize developments in the past 5 days",
        execution_mode="autonomous_local",
        evaluated_at=CLOCK,
    )

    assert spec.time_window.start == (CLOCK - timedelta(days=5)).isoformat()
    assert spec.time_window.end == CLOCK.isoformat()
    assert spec.freshness_requirements[0].max_age_days == 5


def test_non_temporal_fallback_stays_unbounded_but_mode_is_not_agent_led() -> None:
    spec = materialize_smart_fallback_spec(
        "Explain PostgreSQL advisory locks and electric current transformers",
        execution_mode="deterministic_debug",
        evaluated_at=CLOCK,
    )

    assert spec.execution_mode is ExecutionMode.DETERMINISTIC_DEBUG
    assert spec.time_window.start is None
    assert spec.time_window.end is None


@pytest.mark.parametrize(
    "objective",
    (
        "Summarize events since last Tuesday",
        "Compare 2026-08-17 with the latest available material",
        "Summarize the past 5 days from 2026-08-17 through 2026-08-22",
        "Summarize the past 500 days",
        "Summarize changes in the last 5 days",
        "Review changes during August 2026",
        "Review changes from August 17 to August 22, 2026",
        "Summarize the past 5 days and material before 2020",
    ),
)
def test_unsupported_or_ambiguous_temporal_intent_fails_actionably(
    objective: str,
) -> None:
    with pytest.raises(FallbackTemporalError, match="--research-spec"):
        materialize_smart_fallback_spec(
            objective,
            execution_mode="autonomous_local",
            evaluated_at=CLOCK,
        )


def test_plan_window_becomes_non_null_provider_recency_request() -> None:
    query = {
        "freshness_requirement": {
            "start": (CLOCK - timedelta(days=5)).isoformat(),
            "end": CLOCK.isoformat(),
        }
    }

    requested = plan_query_recency_tbs(query, evaluated_at=CLOCK)
    assert requested == "qdr:5d"
    normalized = normalize_recency_window(requested)
    assert normalized is not None
    assert normalized.provider_tbs == "qdr:w"


def test_unbounded_plan_remains_unbounded() -> None:
    assert (
        plan_query_recency_tbs(
            {"freshness_requirement": {"start": None, "end": None}},
            evaluated_at=CLOCK,
        )
        is None
    )


def test_provider_unrepresentable_bounded_plan_fails_instead_of_tbs_null() -> None:
    with pytest.raises(TemporalPlanTransportError, match="unbounded"):
        plan_query_recency_tbs(
            {
                "freshness_requirement": {
                    "start": (CLOCK - timedelta(days=400)).isoformat(),
                    "end": CLOCK.isoformat(),
                }
            },
            evaluated_at=CLOCK,
        )


def test_future_start_fails_instead_of_becoming_one_day_past_recency() -> None:
    with pytest.raises(TemporalPlanTransportError, match="future"):
        plan_query_recency_tbs(
            {
                "freshness_requirement": {
                    "start": (CLOCK + timedelta(days=2)).isoformat(),
                    "end": (CLOCK + timedelta(days=3)).isoformat(),
                }
            },
            evaluated_at=CLOCK,
        )


def test_materialized_range_activates_issue301_publication_authority() -> None:
    spec = materialize_smart_fallback_spec(
        "Review changes from 2026-08-17 through 2026-08-22",
        execution_mode="autonomous_local",
        evaluated_at=CLOCK,
    )
    payload = serialize_model(spec)

    assert passage_temporally_qualifies(
        {"published_at": "2026-08-20T10:00:00Z"},
        payload,
        now=CLOCK,
    )
    assert not passage_temporally_qualifies(
        {"published_at": "2026-08-16T23:59:59Z"},
        payload,
        now=CLOCK,
    )
    assert not passage_temporally_qualifies(
        {"published_at": None, "retrieved_at": "2026-08-20T10:00:00Z"},
        payload,
        now=CLOCK,
    )
    assert not passage_temporally_qualifies(
        {"published_at": "2026-08-23T00:00:00Z"},
        payload,
        now=CLOCK,
    )
