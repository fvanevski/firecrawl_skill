"""Issue #307 regressions for semantic temporal intent and discovery separation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from firecrawl_skill.research_domain import serialize_model
from firecrawl_skill.research_store.fallback_temporal_spec import (
    FallbackTemporalError,
    materialize_smart_fallback_spec,
)
from firecrawl_skill.research_store.plan_recency import plan_query_recency_tbs
from firecrawl_skill.research_store.recency import normalize_recency_window
from firecrawl_skill.research_store.smart_objective_intent import (
    SmartObjectiveIntentError,
    materialize_smart_objective_intent,
    validate_smart_objective_intent,
)
from firecrawl_skill.research_store.smart_search_application import canonical_plan
from firecrawl_skill.research_store.temporal_policy import passage_temporally_qualifies

CLOCK = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def _intent(kind: str, **temporal):
    defaults = {
        "relative_quantity": None,
        "relative_unit": None,
        "freshness_basis": None,
        "publication_start": None,
        "publication_end": None,
        "uncertainty": "none",
        "rationale": "test",
    }
    defaults.update(temporal)
    return {
        "schema_version": "smart-objective-intent-v1",
        "objective": "Latest reporting about Trump and Iran from the past 5 days",
        "temporal": {"kind": kind, **defaults},
        "assumptions": [],
        "ambiguities": [],
    }


def test_audited_latest_past_five_days_is_freshness_not_publication_window() -> None:
    payload = _intent(
        "relative_freshness",
        relative_quantity=5,
        relative_unit="day",
        freshness_basis="publication_or_update",
    )
    materialized = materialize_smart_objective_intent(
        payload, execution_mode="autonomous_local", evaluated_at=CLOCK
    )
    spec = serialize_model(materialized.spec)

    assert spec["time_window"]["start"] is None
    assert spec["time_window"]["end"] is None
    assert spec["freshness_requirements"][0]["max_age_days"] == 5
    assert materialized.discovery_window.start == (CLOCK - timedelta(days=5)).isoformat()
    assert passage_temporally_qualifies(
        {
            "published_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-08-22T00:00:00Z",
        },
        spec,
        now=CLOCK,
    )


def test_explicit_publication_window_remains_publication_only() -> None:
    payload = _intent(
        "absolute_publication_window",
        publication_start="2026-08-18",
        publication_end="2026-08-23",
    )
    materialized = materialize_smart_objective_intent(
        payload, execution_mode="autonomous_local", evaluated_at=CLOCK
    )
    spec = serialize_model(materialized.spec)

    assert spec["time_window"]["start"] == "2026-08-18"
    assert not passage_temporally_qualifies(
        {
            "published_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-08-22T00:00:00Z",
        },
        spec,
        now=CLOCK,
    )


def test_conjunctive_intent_materializes_both_obligations() -> None:
    payload = _intent(
        "conjunctive",
        relative_quantity=2,
        relative_unit="day",
        freshness_basis="publication_or_update",
        publication_start="2026-08-18",
        publication_end="2026-08-23",
    )
    materialized = materialize_smart_objective_intent(
        payload, execution_mode="autonomous_local", evaluated_at=CLOCK
    )
    spec = serialize_model(materialized.spec)
    assert spec["time_window"]["start"] == "2026-08-18"
    assert spec["freshness_requirements"][0]["max_age_days"] == 2


def test_schema_post_validation_rejects_changed_objective_and_ambiguity() -> None:
    payload = _intent("none")
    with pytest.raises(SmartObjectiveIntentError, match="exact raw objective"):
        validate_smart_objective_intent(payload, objective="different")

    payload["ambiguities"] = ["latest has no explicit duration"]
    payload["temporal"]["uncertainty"] = "ambiguous"
    with pytest.raises(SmartObjectiveIntentError, match="ambiguous or unsupported"):
        validate_smart_objective_intent(payload, objective=payload["objective"])


def test_search_plan_discovery_window_is_independent_from_evidence_window() -> None:
    payload = _intent(
        "relative_freshness",
        relative_quantity=5,
        relative_unit="day",
        freshness_basis="publication_or_update",
    )
    materialized = materialize_smart_objective_intent(
        payload, execution_mode="autonomous_local", evaluated_at=CLOCK
    )
    plan = canonical_plan(
        materialized.spec,
        [{"query": "Trump Iran latest", "facet": "news"}],
        discovery_window=materialized.discovery_window,
    )
    query = plan["queries"][0]
    assert query["freshness_requirement"]["start"] == (
        CLOCK - timedelta(days=5)
    ).isoformat()
    requested = plan_query_recency_tbs(query, evaluated_at=CLOCK)
    assert requested == "qdr:5d"
    assert normalize_recency_window(requested).provider_tbs == "qdr:w"


def test_unrepresentable_provider_recency_degrades_to_unbounded_discovery() -> None:
    query = {
        "freshness_requirement": {
            "start": (CLOCK - timedelta(days=500)).isoformat(),
            "end": CLOCK.isoformat(),
        }
    }
    assert plan_query_recency_tbs(query, evaluated_at=CLOCK) is None


def test_degraded_fallback_tolerates_redundant_latest_but_keeps_freshness_only() -> None:
    spec = materialize_smart_fallback_spec(
        "Latest reporting about Trump and Iran from the past 5 days",
        execution_mode="autonomous_local",
        evaluated_at=CLOCK,
    )
    assert spec.time_window.start is None
    assert spec.time_window.end is None
    assert spec.freshness_requirements[0].max_age_days == 5


def test_degraded_fallback_remains_narrow_for_unsupported_temporal_language() -> None:
    with pytest.raises(FallbackTemporalError):
        materialize_smart_fallback_spec(
            "Latest reporting since last Tuesday",
            execution_mode="autonomous_local",
            evaluated_at=CLOCK,
        )
