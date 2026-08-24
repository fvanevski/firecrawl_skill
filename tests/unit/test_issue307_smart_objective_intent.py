"""Issue #307 regressions for semantic temporal intent and discovery separation."""

from __future__ import annotations

import importlib.util
import os
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path
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


def _load_fsearch_smart() -> Any:
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    loader = SourceFileLoader("issue307_fsearch_smart", str(scripts / "fsearch_smart"))
    spec = importlib.util.spec_from_loader("issue307_fsearch_smart", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_query_planner_consumes_materialized_semantic_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart = _load_fsearch_smart()
    captured: dict[str, Any] = {}

    def fake_plan_queries(
        objective: str, brief: dict[str, Any], count: int, *_args: Any, **_kwargs: Any
    ):
        captured.update(objective=objective, brief=brief, count=count)
        return ([{"query": "focused query"}], {"status": "succeeded"})

    monkeypatch.setattr(smart.workflow, "plan_queries", fake_plan_queries)
    research_spec = {
        "questions": [
            {"text": "What changed?"},
            {"text": "What primary evidence supports it?"},
        ],
        "entities": ["Donald Trump", "Iran"],
        "jurisdictions": ["United States", "Iran"],
        "user_constraints": ["Prioritize primary sources."],
        "time_window": {"start": None, "end": None},
        "freshness_requirements": [{"max_age_days": 5}],
    }

    smart.workflow_query_planner(
        "Latest serious reporting about Trump and Iran within the past 5 days",
        4,
        object(),
        {"research_spec": research_spec},
    )

    assert captured["brief"]["questions"] == [
        "What changed?",
        "What primary evidence supports it?",
    ]
    assert captured["brief"]["entities"] == ["Donald Trump", "Iran"]
    assert captured["brief"]["jurisdiction"] == "United States, Iran"
    assert captured["brief"]["user_constraints"] == ["Prioritize primary sources."]
    assert '"max_age_days": 5' in captured["brief"]["time_window"]


def _status(execution_mode: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        lifecycle_revision=1,
        state="created",
        execution_mode=execution_mode,
    )


def _run_service() -> SimpleNamespace:
    return SimpleNamespace(uow_factory=lambda: None)


def _stub_interpreter_failure(message: str) -> Any:
    def _raise(**_kwargs: Any) -> Any:
        raise SmartObjectiveIntentError(message)

    return _raise


def _stub_interpreter_error(message: str) -> Any:
    return lambda **_kwargs: SimpleNamespace(
        value=None,
        error=message,
        provenance={},
        semantic_call_id=None,
        artifact_ids=[],
    )


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (
            "raise",
            "structured intent missing required temporal structure",
        ),
        (
            "raise",
            "schema validation failed for smart-objective-intent-v1",
        ),
        (
            "raise",
            "semantic objective intent is ambiguous or unsupported; provide an explicit ResearchSpec",
        ),
        (
            "error",
            "local semantic provider unavailable",
        ),
    ],
)
def test_autonomous_semantic_failures_stop_before_degradation(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    message: str,
) -> None:
    smart = _load_fsearch_smart()
    stub = _stub_interpreter_failure if failure == "raise" else _stub_interpreter_error
    monkeypatch.setattr(smart, "interpret_smart_objective", stub(message))

    with pytest.raises(FallbackTemporalError, match="--research-spec"):
        smart.resolve_objective_spec(
            path=None,
            topic="Review changes during August 2026",
            status=_status("autonomous_local"),
            run_service=_run_service(),
            invocation_id="issue307-autonomous-fail-closed",
            evaluated_at=CLOCK,
        )


def test_autonomous_semantic_failure_stops_cli_before_orchestrator_execution(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from firecrawl_skill.research_store import smart_orchestrator

    smart = _load_fsearch_smart()
    executed: list[str] = []
    external_id = "fr_" + "b" * 32
    monkeypatch.setattr(
        smart,
        "resolved_research_environment",
        lambda: {
            "DATABASE_URL": "postgresql://test",
            "FIRECRAWL_RESEARCH_AUTO_ENV": "0",
            "PATH": os.environ.get("PATH", ""),
        },
    )
    monkeypatch.setattr(
        smart,
        "prepare_run",
        lambda *_args: (
            external_id,
            object(),
            _run_service(),
            _status("autonomous_local"),
        ),
    )
    monkeypatch.setattr(smart_orchestrator, "load_planning_bundle", lambda *_args: None)
    monkeypatch.setattr(
        smart,
        "execute",
        lambda *_args: executed.append("execute") or SimpleNamespace(outcome="done"),
    )
    monkeypatch.setattr(
        smart,
        "interpret_smart_objective",
        _stub_interpreter_failure(
            "semantic objective intent is ambiguous or unsupported"
        ),
    )

    with pytest.raises(SystemExit) as exit_info:
        smart.main(["Review changes during August 2026"])

    assert exit_info.value.code == 2
    assert executed == []
    assert "--research-spec" in capsys.readouterr().err


def test_deterministic_debug_still_degrades_when_semantic_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart = _load_fsearch_smart()
    monkeypatch.setattr(
        smart,
        "interpret_smart_objective",
        _stub_interpreter_failure(
            "semantic interpreter unavailable in deterministic_debug"
        ),
    )
    status = _status("deterministic_debug")

    spec, _discovery, source, provenance = smart.resolve_objective_spec(
        path=None,
        topic="Explain PostgreSQL advisory locks and electric current transformers",
        status=status,
        run_service=_run_service(),
        invocation_id="issue307-deterministic-debug",
        evaluated_at=CLOCK,
    )

    assert source == "deterministic degraded fallback"
    assert provenance["authority"] == "deterministic_degraded_fallback"
    assert "semantic interpreter unavailable" in provenance["semantic_error"]
    assert spec.time_window.start is None
    assert spec.time_window.end is None


def test_deterministic_debug_accepts_sanctioned_redundant_freshness_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart = _load_fsearch_smart()
    monkeypatch.setattr(
        smart,
        "interpret_smart_objective",
        _stub_interpreter_failure(
            "semantic interpreter unavailable in deterministic_debug"
        ),
    )

    spec, _discovery, source, _provenance = smart.resolve_objective_spec(
        path=None,
        topic="Latest reporting about Trump and Iran from the past 5 days",
        status=_status("deterministic_debug"),
        run_service=_run_service(),
        invocation_id="issue307-deterministic-debug-redundant",
        evaluated_at=CLOCK,
    )

    assert source == "deterministic degraded fallback"
    assert spec.time_window.start is None
    assert spec.time_window.end is None
    assert spec.freshness_requirements[0].max_age_days == 5
