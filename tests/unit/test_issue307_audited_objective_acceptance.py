"""Issue #307 exact audited-objective semantic acceptance regression."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from firecrawl_skill.research_domain import serialize_model
from firecrawl_skill.research_store.smart_objective_intent import (
    materialize_smart_objective_intent,
)

CLOCK = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
OBJECTIVE = (
    "Latest serious news and reporting within the past 5 days about Donald Trump "
    "and Iran, prioritizing primary sources and independent corroboration."
)


def test_exact_audited_language_materializes_freshness_and_semantic_scope() -> None:
    payload = {
        "schema_version": "smart-objective-intent-v1",
        "objective": OBJECTIVE,
        "research_questions": [
            "What material developments involving Donald Trump and Iran occurred or were authoritatively updated within the past five days?",
            "Which developments are corroborated by primary or independent authoritative sources?",
        ],
        "entities": ["Donald Trump", "Iran"],
        "jurisdictions": ["United States", "Iran"],
        "user_constraints": [
            "Prioritize primary sources and independent corroboration.",
            "Use serious news and reporting.",
        ],
        "temporal": {
            "kind": "relative_freshness",
            "relative_quantity": 5,
            "relative_unit": "day",
            "freshness_basis": "publication_or_update",
            "publication_start": None,
            "publication_end": None,
            "uncertainty": "none",
            "rationale": "latest plus past five days expresses rolling freshness",
        },
        "assumptions": [],
        "ambiguities": [],
    }

    materialized = materialize_smart_objective_intent(
        payload,
        execution_mode="autonomous_local",
        evaluated_at=CLOCK,
    )
    spec = serialize_model(materialized.spec)

    assert spec["objective"] == OBJECTIVE
    assert spec["time_window"]["start"] is None
    assert spec["time_window"]["end"] is None
    assert spec["freshness_requirements"][0]["max_age_days"] == 5
    assert materialized.discovery_window.start == (CLOCK - timedelta(days=5)).isoformat()
    assert [question["text"] for question in spec["questions"]] == payload[
        "research_questions"
    ]
    assert spec["entities"] == ["Donald Trump", "Iran"]
    assert spec["jurisdictions"] == ["United States", "Iran"]
    assert spec["user_constraints"] == payload["user_constraints"]
