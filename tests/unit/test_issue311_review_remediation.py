"""Independent-review regressions for issue #311 deterministic query authority."""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.budget_policy import conservative_research_spec
from firecrawl_skill.research_store.query_policy import (
    materialize_query_plan,
    validate_query_proposal_payload,
)
from firecrawl_skill.research_store.smart_objective_intent import (
    unbounded_discovery_window,
)


def _proposal(spec, query: str, *, facet: str = "authority") -> dict[str, object]:
    return {
        "query": query,
        "facet": facet,
        "target_question_ids": [str(spec.questions[0].question_id)],
        "target_claim_ids": [],
        "intended_source_class": "primary",
        "expected_organizations": [],
        "expected_contribution": "direct evidence",
    }


def test_query_priority_and_order_are_invariant_to_proposal_permutation() -> None:
    spec = conservative_research_spec("permutation-stable query policy", "general")
    run_id = uuid4()
    proposals = [
        _proposal(spec, "zeta evidence", facet="secondary"),
        _proposal(spec, "alpha evidence", facet="primary"),
    ]

    first = materialize_query_plan(
        spec,
        deepcopy(proposals),
        run_id=run_id,
        discovery_window=unbounded_discovery_window(),
        max_queries=2,
    )
    second = materialize_query_plan(
        spec,
        list(reversed(deepcopy(proposals))),
        run_id=run_id,
        discovery_window=unbounded_discovery_window(),
        max_queries=2,
    )

    assert first == second
    assert [item["query"] for item in first["queries"]] == [
        "alpha evidence",
        "zeta evidence",
    ]
    assert [item["priority"] for item in first["queries"]] == [1, 2]


def test_materializer_fails_closed_before_query_cap_can_hide_model_order() -> None:
    spec = conservative_research_spec("bounded query policy", "general")

    with pytest.raises(ValueError, match="contains 2 queries; cap is 1"):
        materialize_query_plan(
            spec,
            [_proposal(spec, "alpha evidence"), _proposal(spec, "zeta evidence")],
            run_id=uuid4(),
            discovery_window=unbounded_discovery_window(),
            max_queries=1,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("query", 42, "query must be a string"),
        ("facet", 42, "facet must be a string"),
        ("intended_source_class", 42, "intended_source_class must be a string"),
        ("expected_contribution", 42, "expected_contribution must be a string"),
        ("expected_organizations", [42], "expected_organizations values must be strings"),
        ("target_question_ids", [42], "target_question_ids values must be strings"),
    ],
)
def test_manual_validator_does_not_coerce_nonsemantic_types(
    field: str,
    value: object,
    message: str,
) -> None:
    spec = conservative_research_spec("strict query validation", "general")
    proposal = _proposal(spec, "strict query validation")
    proposal[field] = value

    with pytest.raises(ValueError, match=message):
        validate_query_proposal_payload(
            {
                "schema_version": "search-query-proposal-v1",
                "queries": [proposal],
            },
            spec,
            max_queries=2,
        )
