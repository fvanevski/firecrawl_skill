"""Issue #307 regressions for semantic temporal intent and discovery separation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

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
        "research_questions": [
            "What are the latest material developments involving Trump and Iran?"
        ],
        "entities": ["Donald Trump", "Iran"],
        "jurisdictions": ["United States", "Iran"],
        "user_constraints": [
            "Prioritize serious reporting and primary sources where available."
        ],
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
    assert (
        materialized.discovery_window.start == (CLOCK - timedelta(days=5)).isoformat()
    )
    assert passage_temporally_qualifies(
        {
            "published_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-08-22T00:00:00Z",
        },
        spec,
        now=CLOCK,
    )


def test_semantic_dimensions_materialize_into_research_spec() -> None:
    payload = _intent(
        "relative_freshness",
        relative_quantity=5,
        relative_unit="day",
        freshness_basis="publication_or_update",
    )
    payload["research_questions"] = [
        "What changed in the last five days?",
        "Which claims are supported by primary or independent reporting?",
    ]
    payload["entities"] = ["Donald Trump", "Iran", "United States"]
    payload["jurisdictions"] = ["United States", "Iran"]
    payload["user_constraints"] = [
        "Prioritize primary sources.",
        "Separate confirmed developments from speculation.",
    ]

    materialized = materialize_smart_objective_intent(
        payload, execution_mode="autonomous_local", evaluated_at=CLOCK
    )
    spec = serialize_model(materialized.spec)

    assert [item["text"] for item in spec["questions"]] == payload["research_questions"]
    assert spec["entities"] == payload["entities"]
    assert spec["jurisdictions"] == payload["jurisdictions"]
    assert spec["user_constraints"] == payload["user_constraints"]
    assert len({item["question_id"] for item in spec["questions"]}) == 2


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


def test_semantic_dimension_validation_rejects_missing_or_duplicate_questions() -> None:
    payload = _intent("none")
    payload["research_questions"] = []
    with pytest.raises(
        SmartObjectiveIntentError, match="at least one research question"
    ):
        validate_smart_objective_intent(payload, objective=payload["objective"])

    payload["research_questions"] = ["What changed?", "  what   changed? "]
    with pytest.raises(SmartObjectiveIntentError, match="duplicate"):
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
    assert (
        query["freshness_requirement"]["start"]
        == (CLOCK - timedelta(days=5)).isoformat()
    )
    requested = plan_query_recency_tbs(query, evaluated_at=CLOCK)
    assert requested == "qdr:5d"
    window = normalize_recency_window(requested)
    assert window is not None
    assert window.provider_tbs == "qdr:w"


def test_unrepresentable_provider_recency_degrades_to_unbounded_discovery() -> None:
    query = {
        "freshness_requirement": {
            "start": (CLOCK - timedelta(days=500)).isoformat(),
            "end": CLOCK.isoformat(),
        }
    }
    assert plan_query_recency_tbs(query, evaluated_at=CLOCK) is None


def test_degraded_fallback_tolerates_redundant_latest_but_keeps_freshness_only() -> (
    None
):
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


def test_controller_semantic_error_fails_before_planning_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from firecrawl_skill.research_store import research_controller as controller_module
    from firecrawl_skill.research_store.research_controller import (
        ControllerPolicy,
        ResearchWorkflowController,
    )
    from firecrawl_skill.research_store.research_controller_contract import (
        ControllerBlockedError,
    )

    controller = ResearchWorkflowController.__new__(ResearchWorkflowController)
    controller.semantic_service = object()
    controller.run_service = SimpleNamespace()
    status = SimpleNamespace(
        id=uuid4(),
        objective="Review changes during August 2026",
        execution_mode="autonomous_local",
    )
    policy = ControllerPolicy(retained_only=False, evaluated_at=CLOCK)
    invocation = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(
        controller_module,
        "interpret_smart_objective",
        lambda **_kwargs: SimpleNamespace(
            value=None,
            error="local semantic provider unavailable",
            provenance={},
            semantic_call_id=None,
            artifact_ids=(),
        ),
    )

    with pytest.raises(ControllerBlockedError, match="semantic objective interpretation failed"):
        controller._persist_planning(status, policy, invocation)


def test_deterministic_debug_fixture_uses_same_versioned_semantic_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from firecrawl_skill.research_store import smart_objective_intent as intent_module

    def fixture_transport(**kwargs: Any) -> SimpleNamespace:
        fixture = kwargs["deterministic_fixture"]
        kwargs["post_validate"](fixture)
        return SimpleNamespace(
            value=fixture,
            error=None,
            provenance={"authority": "deterministic_debug_fixture"},
            semantic_call_id=None,
            artifact_ids=(),
        )

    monkeypatch.setattr(intent_module, "call_authorized_structured", fixture_transport)
    status = SimpleNamespace(
        id=uuid4(),
        lifecycle_revision=1,
        execution_mode="deterministic_debug",
    )
    objective = "Latest reporting about Trump and Iran from the past 5 days"
    interpreted = intent_module.interpret_smart_objective(
        semantic_service=SimpleNamespace(host_artifact_supplier=None),
        status=status,
        objective=objective,
        invocation_id="issue307-deterministic-debug",
        evaluated_at=CLOCK,
    )
    materialized = materialize_smart_objective_intent(
        interpreted.value,
        execution_mode=status.execution_mode,
        evaluated_at=CLOCK,
    )
    assert materialized.spec.time_window.start is None
    assert materialized.spec.time_window.end is None
    assert materialized.spec.freshness_requirements[0].max_age_days == 5
    assert interpreted.provenance["authority"] == "deterministic_debug_fixture"
