"""Issue #371 regressions for typed temporal basis and bounded resolution."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from firecrawl_skill.research_domain import serialize_model
from firecrawl_skill.research_store.candidate_temporal_policy import (
    assess_candidate_temporal,
)
from firecrawl_skill.research_store.smart_objective_intent import (
    SmartObjectiveIntentError,
    materialize_smart_objective_intent,
    validate_smart_objective_intent,
)
from firecrawl_skill.research_store.evidence_preparation_service import (
    partition_temporal_passages,
)
from firecrawl_skill.research_store.smart_search_application import canonical_plan
from firecrawl_skill.research_store.temporal_coverage import diagnose_temporal_coverage
from firecrawl_skill.research_store.temporal_policy import (
    passage_temporal_qualification,
)
from firecrawl_skill.research_store.temporal_resolution import (
    MAX_TEMPORAL_PROVENANCE_PROBES_PER_RUN,
    resolve_document_temporal_provenance,
)

CLOCK = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _intent(kind: str, *, objective: str = "temporal test", **temporal):
    basis = {
        "none": "none",
        "relative_freshness": "publication_or_update_within",
        "relative_publication_window": "publication_within",
        "absolute_publication_window": "publication_within",
        "event_window": "event_within",
        "current_as_of": "current_as_of",
        "conjunctive": "conjunctive",
    }[kind]
    fields = {
        "relative_quantity": None,
        "relative_unit": None,
        "freshness_basis": None,
        "temporal_basis": basis,
        "publication_start": None,
        "publication_end": None,
        "event_start": None,
        "event_end": None,
        "as_of": None,
        "uncertainty": "none",
        "rationale": "issue 371 regression",
    }
    fields.update(temporal)
    return {
        "schema_version": "smart-objective-intent-v2",
        "objective": objective,
        "research_questions": [objective],
        "entities": [],
        "jurisdictions": [],
        "user_constraints": [],
        "temporal": {"kind": kind, **fields},
        "assumptions": [],
        "ambiguities": [],
    }


def _spec(payload):
    return serialize_model(
        materialize_smart_objective_intent(
            payload,
            execution_mode="autonomous_local",
            evaluated_at=CLOCK,
        ).spec
    )


def test_publication_or_update_relative_wording_cannot_be_misclassified_conjunctive() -> (
    None
):
    objective = (
        "Using https://example.com as the canonical source, state the purpose of "
        "Example Domain, but require authoritative publication or update within "
        "the last 30 days. Retrieval time alone must not satisfy the temporal "
        "requirement."
    )
    wrong = _intent(
        "conjunctive",
        objective=objective,
        relative_quantity=30,
        relative_unit="day",
        freshness_basis="publication_or_update",
        publication_start="2026-08-12",
        publication_end="2026-09-11",
    )
    with pytest.raises(
        SmartObjectiveIntentError,
        match="relative publication-or-update wording",
    ):
        validate_smart_objective_intent(wrong, objective=objective)

    correct = _intent(
        "relative_freshness",
        objective=objective,
        relative_quantity=30,
        relative_unit="day",
        freshness_basis="publication_or_update",
    )
    validate_smart_objective_intent(correct, objective=objective)
    spec = _spec(correct)
    assert spec["temporal_basis"] == "publication_or_update_within"
    assert spec["time_window"]["start"] is None
    assert spec["freshness_requirements"][0]["max_age_days"] == 30


def test_old_publication_plus_recent_update_satisfies_freshness() -> None:
    spec = _spec(
        _intent(
            "relative_freshness",
            relative_quantity=90,
            relative_unit="day",
            freshness_basis="publication_or_update",
        )
    )
    result = passage_temporal_qualification(
        {
            "published_at": "2020-01-01T00:00:00Z",
            "updated_at": "2026-09-09T00:00:00Z",
        },
        spec,
        now=CLOCK,
    )
    assert result.status == "satisfies"
    assert result.basis == "publication_or_update_within"


def test_old_publication_with_unknown_update_remains_unresolved() -> None:
    spec = _spec(
        _intent(
            "relative_freshness",
            relative_quantity=90,
            relative_unit="day",
            freshness_basis="publication_or_update",
        )
    )
    result = passage_temporal_qualification(
        {"published_at": "2020-01-01T00:00:00Z"}, spec, now=CLOCK
    )
    assert result.status == "unresolved"
    assert result.reason == "missing_update_authority"

    candidate = assess_candidate_temporal(
        {
            "published_at": "2020-01-01T00:00:00Z",
            "date_signals": {
                "publication_status": "explicit_provider_valid",
                "update_status": "unknown",
            },
        },
        spec,
        now=CLOCK,
    )
    assert candidate.status == "unknown"


def test_publication_only_requires_publication_even_when_update_is_recent() -> None:
    spec = _spec(
        _intent(
            "absolute_publication_window",
            publication_start="2026-08-01",
            publication_end="2026-08-31",
        )
    )
    result = passage_temporal_qualification(
        {
            "published_at": "2020-01-01T00:00:00Z",
            "updated_at": "2026-08-20T00:00:00Z",
        },
        spec,
        now=CLOCK,
    )
    assert result.status == "violates"
    assert result.reason == "explicit_publication_out_of_window"


def test_event_basis_uses_event_time_not_article_publication() -> None:
    spec = _spec(
        _intent(
            "event_window",
            event_start="2026-08-01",
            event_end="2026-08-31",
        )
    )
    result = passage_temporal_qualification(
        {
            "published_at": "2026-09-05T00:00:00Z",
            "temporal_provenance": {
                "event_at": "2026-08-12T00:00:00Z",
                "event_status": "explicit_valid",
                "event_authority": "github_issue_pr_opened",
            },
        },
        spec,
        now=CLOCK,
    )
    assert result.status == "satisfies"
    assert result.basis == "event_within"


def test_event_time_without_source_qualified_authority_remains_unresolved() -> None:
    spec = _spec(
        _intent(
            "event_window",
            event_start="2026-08-01",
            event_end="2026-08-31",
        )
    )
    result = passage_temporal_qualification(
        {
            "temporal_provenance": {
                "event_at": "2026-08-12T00:00:00Z",
                "event_status": "explicit_valid",
                "event_authority": "none",
            }
        },
        spec,
        now=CLOCK,
    )
    assert result.status == "unresolved"
    assert result.reason == "event_time_unresolved"


def test_event_basis_never_projects_publication_recency_to_search() -> None:
    materialized = materialize_smart_objective_intent(
        _intent(
            "event_window",
            event_start="2026-08-01",
            event_end="2026-08-31",
        ),
        execution_mode="autonomous_local",
        evaluated_at=CLOCK,
    )
    plan = canonical_plan(
        materialized.spec,
        [{"query": "August event record", "facet": "primary"}],
        discovery_window=materialized.discovery_window,
    )
    query = plan["queries"][0]
    assert query["temporal_discovery_mode"] == "non_narrowing"
    assert query["freshness_requirement"]["start"] is None
    assert query["freshness_requirement"]["end"] is None


def test_publication_or_update_plan_reserves_non_narrowing_branch_within_cap() -> None:
    materialized = materialize_smart_objective_intent(
        _intent(
            "relative_freshness",
            relative_quantity=90,
            relative_unit="day",
            freshness_basis="publication_or_update",
        ),
        execution_mode="autonomous_local",
        evaluated_at=CLOCK,
    )
    plan = canonical_plan(
        materialized.spec,
        [
            {
                "query": "site:example.org canonical status",
                "facet": "primary",
                "intended_source_class": "official",
                "expected_organizations": ["Example"],
                "expected_contribution": "canonical source status",
            },
            {"query": "example status recent", "facet": "news"},
        ],
        discovery_window=materialized.discovery_window,
    )
    assert len(plan["queries"]) == 2
    modes = [item["temporal_discovery_mode"] for item in plan["queries"]]
    assert modes.count("non_narrowing") == 1
    assert modes.count("recency_constrained") == 1
    non_narrowing = next(
        item
        for item in plan["queries"]
        if item["temporal_discovery_mode"] == "non_narrowing"
    )
    assert non_narrowing["freshness_requirement"]["start"] is None
    assert "reserved" in non_narrowing["temporal_discovery_reason"]


def test_github_source_specific_resolution_produces_event_and_state_observation() -> (
    None
):
    document = {
        "publication_signals": [
            {
                "source": "github_issue_pr_opened_marker",
                "status": "valid",
                "parsed": "2026-08-12T00:00:00+00:00",
            }
        ],
        "update_signals": [],
        "source_semantics": {"source_kind": "github_issue_or_pr"},
    }
    result = resolve_document_temporal_provenance(
        document,
        retrieved_at=datetime(2026, 9, 7, 16, 0, tzinfo=timezone.utc),
        run_probe_ordinal=1,
    )
    assert result["attempt_count"] == 1
    assert result["source_specific_action_count"] == 1
    assert result["event_status"] == "explicit_valid"
    assert result["event_at"] == "2026-08-12T00:00:00+00:00"
    assert result["state_observed_at"] == "2026-09-07T16:00:00+00:00"
    assert result["exhausted"] is True


def test_current_as_of_accepts_canonical_source_state_observed_that_day() -> None:
    spec = _spec(_intent("current_as_of", as_of="2026-09-07"))
    result = passage_temporal_qualification(
        {
            "temporal_provenance": {
                "state_observed_at": "2026-09-07T16:00:00Z",
                "state_authority": "github_issue_pr_snapshot_observation",
            }
        },
        spec,
        now=CLOCK,
    )
    assert result.status == "satisfies"
    assert result.basis == "current_as_of"


def test_current_as_of_requires_source_qualified_state_authority() -> None:
    spec = _spec(_intent("current_as_of", as_of="2026-09-07"))
    result = passage_temporal_qualification(
        {
            "temporal_provenance": {
                "state_observed_at": "2026-09-07T16:00:00Z",
                "state_authority": "none",
            }
        },
        spec,
        now=CLOCK,
    )
    assert result.status == "unresolved"
    assert result.reason == "as_of_state_unresolved"


def test_current_as_of_exact_datetime_is_a_point_not_an_empty_window() -> None:
    spec = _spec(_intent("current_as_of", as_of="2026-09-07T16:00:00+00:00"))
    result = passage_temporal_qualification(
        {
            "temporal_provenance": {
                "state_observed_at": "2026-09-07T16:00:00Z",
                "state_authority": "github_issue_pr_snapshot_observation",
            }
        },
        spec,
        now=CLOCK,
    )
    assert result.status == "satisfies"
    assert result.reason == "source_state_observed_at_requested_as_of_time"


def test_historical_temporal_failure_is_retained_as_context_only() -> None:
    spec = _spec(
        _intent(
            "absolute_publication_window",
            publication_start="2026-08-01",
            publication_end="2026-08-31",
        )
    )
    historical = {
        "chunk_id": "historical",
        "published_at": "2020-01-01T00:00:00Z",
    }
    current = {
        "chunk_id": "current",
        "published_at": "2026-08-20T00:00:00Z",
    }
    qualifying, context_only = partition_temporal_passages(
        [historical, current],
        spec,
        now=CLOCK,
    )
    assert [item["chunk_id"] for item in qualifying] == ["current"]
    assert [item["chunk_id"] for item in context_only] == ["historical"]
    assert (
        passage_temporal_qualification(historical, spec, now=CLOCK).status == "violates"
    )


def test_conflicting_explicit_temporal_authority_is_unresolved_not_violated() -> None:
    spec = _spec(
        _intent(
            "relative_freshness",
            relative_quantity=90,
            relative_unit="day",
            freshness_basis="publication_or_update",
        )
    )
    result = passage_temporal_qualification(
        {
            "published_at": "2026-09-09T00:00:00Z",
            "temporal_provenance": {
                "publication_status": "explicit_conflict",
                "update_status": "unknown",
                "resolution": {"exhausted": True},
            },
        },
        spec,
        now=CLOCK,
    )
    assert result.status == "unresolved"
    diagnostics = diagnose_temporal_coverage(
        [
            {
                "published_at": "2026-09-09T00:00:00Z",
                "temporal_provenance": {
                    "publication_status": "explicit_conflict",
                    "resolution": {"exhausted": True},
                },
            }
        ],
        spec,
        now=CLOCK,
    )
    assert diagnostics.unresolved_passages == 1
    assert diagnostics.invalid_or_conflicting_authority == 1
    assert diagnostics.provenance_resolution_exhausted == 1


def test_retrieval_only_timestamp_never_becomes_publication_or_update() -> None:
    spec = _spec(
        _intent(
            "relative_freshness",
            relative_quantity=90,
            relative_unit="day",
            freshness_basis="publication_or_update",
        )
    )
    passage = {"retrieved_at": "2026-09-09T00:00:00Z"}
    result = passage_temporal_qualification(passage, spec, now=CLOCK)
    assert result.status == "unresolved"
    diagnostics = diagnose_temporal_coverage([passage], spec, now=CLOCK)
    assert diagnostics.retrieval_only_passages == 1


def test_finite_resolution_budget_exhaustion_does_not_probe_again() -> None:
    result = resolve_document_temporal_provenance(
        {
            "publication_signals": [],
            "update_signals": [],
            "source_semantics": {"source_kind": "generic"},
        },
        retrieved_at=CLOCK,
        run_probe_ordinal=MAX_TEMPORAL_PROVENANCE_PROBES_PER_RUN + 1,
    )
    assert result["attempted"] is False
    assert result["attempt_count"] == 0
    assert result["source_specific_action_count"] == 0
    assert result["exhausted"] is True
    assert result["exhaustion_reason"] == "run_probe_budget_exhausted"
