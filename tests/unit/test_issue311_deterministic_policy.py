"""Issue #311 deterministic planner and candidate-selection regressions."""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.budget_policy import conservative_research_spec
from firecrawl_skill.research_store.candidate_selection_policy import (
    CANDIDATE_LABEL_SCHEMA,
    select_candidates,
    validate_candidate_label_payload,
)
from firecrawl_skill.research_store.query_policy import (
    QUERY_PROPOSAL_SCHEMA,
    materialize_query_plan,
    parse_query_structure,
    validate_query_proposal_payload,
)
from firecrawl_skill.research_store.semantic_service import validate_structured_payload
from firecrawl_skill.research_store.smart_objective_intent import (
    unbounded_discovery_window,
)


def _spec():
    return conservative_research_spec("deterministic planning evidence", "general")


def _proposal(spec, query: str) -> dict[str, object]:
    return {
        "query": query,
        "facet": "authority",
        "target_question_ids": [str(spec.questions[0].question_id)],
        "target_claim_ids": [],
        "intended_source_class": "primary",
        "expected_organizations": [],
        "expected_contribution": "direct evidence",
    }


@pytest.mark.parametrize(
    "forbidden",
    [
        "freshness_requirement",
        "domain_neutral",
        "priority",
        "query_id",
        "provider_recency",
    ],
)
def test_query_semantic_schema_rejects_application_owned_fields(forbidden: str) -> None:
    spec = _spec()
    payload = {
        "schema_version": "search-query-proposal-v1",
        "queries": [_proposal(spec, "deterministic evidence")],
    }
    payload["queries"][0][forbidden] = True

    assert any(
        f"unexpected field {forbidden}" in error
        for error in validate_structured_payload(payload, QUERY_PROPOSAL_SCHEMA)
    )
    with pytest.raises(ValueError, match="non-semantic fields"):
        validate_query_proposal_payload(payload, spec, max_queries=4)


def test_query_structure_is_parsed_from_actual_site_operators() -> None:
    parsed = parse_query_structure(
        "deterministic evidence site:EPA.GOV -site:example.com"
    )

    assert parsed["domain_restrictions"] == ["epa.gov"]
    assert parsed["negative_terms"] == ["site:example.com"]
    assert parsed["is_domain_scoped"] is True


def test_parenthesized_site_operators_cannot_spoof_domain_neutrality() -> None:
    parsed = parse_query_structure(
        "deterministic evidence (site:EPA.GOV OR site:NOAA.GOV)"
    )

    assert parsed["domain_restrictions"] == ["epa.gov", "noaa.gov"]
    assert parsed["is_domain_scoped"] is True


@pytest.mark.parametrize(
    "operator",
    ["after:2026-01-01", "before:2026-02-01", "qdr:m", "tbs:qdr:m"],
)
def test_query_proposal_rejects_application_owned_temporal_provider_operators(
    operator: str,
) -> None:
    with pytest.raises(ValueError, match="application-owned search operator"):
        parse_query_structure(f"deterministic evidence {operator}")


def test_all_scoped_proposals_receive_real_unscoped_fallback_within_cap() -> None:
    spec = _spec()
    plan = materialize_query_plan(
        spec,
        [
            _proposal(spec, "evidence site:epa.gov"),
            _proposal(spec, "authority site:noaa.gov"),
        ],
        run_id=uuid4(),
        discovery_window=unbounded_discovery_window(),
        max_queries=2,
    )

    assert len(plan["queries"]) == 2
    assert plan["queries"][0]["domain_restrictions"] == ["epa.gov"]
    fallback = plan["queries"][1]
    assert fallback["domain_restrictions"] == []
    assert "site:" not in fallback["query"].casefold()
    assert fallback["facet"] == "unscoped_objective_fallback"


def test_query_duplicate_targets_and_duplicate_normalized_queries_fail_closed() -> None:
    spec = _spec()
    duplicate_target = _proposal(spec, "deterministic evidence")
    question_id = str(spec.questions[0].question_id)
    duplicate_target["target_question_ids"] = [question_id, question_id]
    with pytest.raises(ValueError, match="duplicate value"):
        validate_query_proposal_payload(
            {
                "schema_version": "search-query-proposal-v1",
                "queries": [duplicate_target],
            },
            spec,
            max_queries=2,
        )

    with pytest.raises(ValueError, match="duplicate normalized query"):
        validate_query_proposal_payload(
            {
                "schema_version": "search-query-proposal-v1",
                "queries": [
                    _proposal(spec, "deterministic   evidence"),
                    _proposal(spec, "Deterministic evidence"),
                ],
            },
            spec,
            max_queries=2,
        )


def test_unscoped_fallback_strips_parenthesized_site_syntax_from_spec_text() -> None:
    spec = conservative_research_spec(
        "deterministic evidence (site:epa.gov OR site:noaa.gov)",
        "general",
    )
    plan = materialize_query_plan(
        spec,
        [_proposal(spec, "authority site:epa.gov")],
        run_id=uuid4(),
        discovery_window=unbounded_discovery_window(),
        max_queries=2,
    )

    fallback = plan["queries"][1]
    assert fallback["domain_restrictions"] == []
    assert "site:" not in fallback["query"].casefold()
    assert fallback["query"] == "deterministic evidence"


def test_query_targets_must_exist_in_persisted_research_spec() -> None:
    spec = _spec()
    proposal = _proposal(spec, "deterministic evidence")
    proposal["target_question_ids"] = [str(uuid4())]

    with pytest.raises(ValueError, match="unknown persisted targets"):
        materialize_query_plan(
            spec,
            [proposal],
            run_id=uuid4(),
            discovery_window=unbounded_discovery_window(),
            max_queries=1,
        )


def test_query_materialization_is_repeatable_for_same_run_and_semantics() -> None:
    spec = _spec()
    run_id = uuid4()
    proposals = [_proposal(spec, "deterministic evidence site:epa.gov")]

    first = materialize_query_plan(
        spec,
        deepcopy(proposals),
        run_id=run_id,
        discovery_window=unbounded_discovery_window(),
        max_queries=2,
    )
    second = materialize_query_plan(
        spec,
        deepcopy(proposals),
        run_id=run_id,
        discovery_window=unbounded_discovery_window(),
        max_queries=2,
    )

    assert first == second
    assert [item["priority"] for item in first["queries"]] == [1, 2]


def _assessment(status: str) -> dict[str, object]:
    return {
        "status": status,
        "basis": "publication_window",
        "reason": "fixture",
        "published_at": None,
        "updated_at": None,
        "publication_status": "unknown",
        "update_status": "unknown",
    }


def _label(candidate_id: str, *, relevance: str = "high") -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "relevance": relevance,
        "source_suitability": "primary",
        "target_question_ids": [],
        "evidence_role": "direct",
        "rationale": "semantic label only",
    }


@pytest.mark.parametrize(
    "forbidden,value",
    [
        ("freshness", "current"),
        ("temporal_eligibility", "eligible"),
        ("scrape", True),
        ("priority", 100),
        ("numeric_priority", 100),
        ("independence", "independent"),
    ],
)
def test_candidate_semantic_schema_rejects_operational_policy_fields(
    forbidden: str, value: object
) -> None:
    spec = _spec()
    candidates = [
        {
            "candidate_id": "cand-a",
            "canonical_url": "https://example.org/a",
            "rank": 1,
            "temporal_assessment": _assessment("eligible"),
        }
    ]
    label = _label("cand-a")
    label[forbidden] = value
    payload = {
        "schema_version": "candidate-semantic-labels-v1",
        "labels": [label],
    }

    assert any(
        f"unexpected field {forbidden}" in error
        for error in validate_structured_payload(payload, CANDIDATE_LABEL_SCHEMA)
    )
    with pytest.raises(ValueError, match="non-semantic fields"):
        validate_candidate_label_payload(payload, candidates, spec)


def test_unknown_candidate_label_fails_closed() -> None:
    spec = _spec()
    candidates = [
        {
            "candidate_id": "cand-a",
            "canonical_url": "https://example.org/a",
            "rank": 1,
            "temporal_assessment": _assessment("eligible"),
        }
    ]
    payload = {
        "schema_version": "candidate-semantic-labels-v1",
        "labels": [_label("cand-unknown")],
    }
    with pytest.raises(ValueError, match="unknown candidate"):
        validate_candidate_label_payload(payload, candidates, spec)


def test_absent_provider_rank_never_makes_input_order_authoritative() -> None:
    candidates = [
        {
            "candidate_id": "cand-b",
            "canonical_url": "https://b.example/article",
            "temporal_assessment": _assessment("eligible"),
        },
        {
            "candidate_id": "cand-a",
            "canonical_url": "https://a.example/article",
            "temporal_assessment": _assessment("eligible"),
        },
    ]
    labels = [_label("cand-a"), _label("cand-b")]

    first = select_candidates(candidates, labels, max_selected=2)
    second = select_candidates(list(reversed(candidates)), labels, max_selected=2)

    assert first.to_dict() == second.to_dict()
    assert [item["candidate_id"] for item in first.selected_candidates] == [
        "cand-a",
        "cand-b",
    ]


def test_temporal_ineligibility_cannot_be_overridden_by_semantic_labels() -> None:
    candidates = [
        {
            "candidate_id": "cand-ineligible",
            "canonical_url": "https://best.example/a",
            "rank": 1,
            "temporal_assessment": _assessment("ineligible"),
        },
        {
            "candidate_id": "cand-unknown",
            "canonical_url": "https://other.example/b",
            "rank": 2,
            "temporal_assessment": _assessment("unknown"),
        },
    ]
    labels = [_label("cand-ineligible"), _label("cand-unknown")]

    selected = select_candidates(candidates, labels, max_selected=2)

    assert [item["candidate_id"] for item in selected.selected_candidates] == [
        "cand-unknown"
    ]
    ineligible = next(
        item for item in selected.decisions if item.candidate_id == "cand-ineligible"
    )
    assert ineligible.selected is False
    assert "temporal admission" in ineligible.reason


def test_identical_persisted_inputs_and_labels_select_identically_when_shuffled() -> (
    None
):
    candidates = [
        {
            "candidate_id": "cand-a",
            "canonical_url": "https://same.example/a",
            "rank": 1,
            "temporal_assessment": _assessment("eligible"),
        },
        {
            "candidate_id": "cand-b",
            "canonical_url": "https://same.example/b",
            "rank": 2,
            "temporal_assessment": _assessment("eligible"),
        },
        {
            "candidate_id": "cand-c",
            "canonical_url": "https://different.example/c",
            "rank": 3,
            "temporal_assessment": _assessment("eligible"),
        },
    ]
    labels = [_label("cand-a"), _label("cand-b"), _label("cand-c")]

    first = select_candidates(candidates, labels, max_selected=2)
    second = select_candidates(
        list(reversed(candidates)),
        list(reversed(labels)),
        max_selected=2,
    )

    assert first.to_dict() == second.to_dict()
    assert [item["candidate_id"] for item in first.selected_candidates] == [
        "cand-a",
        "cand-c",
    ]


def test_semantic_unrelated_label_is_a_bounded_exclusion_not_numeric_priority() -> None:
    candidates = [
        {
            "candidate_id": "cand-a",
            "canonical_url": "https://a.example/a",
            "rank": 1,
            "temporal_assessment": _assessment("eligible"),
        },
        {
            "candidate_id": "cand-b",
            "canonical_url": "https://b.example/b",
            "rank": 2,
            "temporal_assessment": _assessment("eligible"),
        },
    ]
    selection = select_candidates(
        candidates,
        [_label("cand-a", relevance="unrelated"), _label("cand-b")],
        max_selected=2,
    )

    assert [item["candidate_id"] for item in selection.selected_candidates] == [
        "cand-b"
    ]


def test_query_policy_rejects_unsupported_search_operator() -> None:
    with pytest.raises(ValueError, match="unsupported search operator"):
        parse_query_structure("deterministic evidence filetype:pdf")


def test_canonical_duplicate_cannot_consume_second_selection_slot() -> None:
    candidates = [
        {
            "candidate_id": "cand-a",
            "canonical_url": "https://same.example/article",
            "rank": 1,
            "temporal_assessment": _assessment("eligible"),
        },
        {
            "candidate_id": "cand-b",
            "canonical_url": "https://same.example/article",
            "rank": 2,
            "temporal_assessment": _assessment("eligible"),
        },
        {
            "candidate_id": "cand-c",
            "canonical_url": "https://other.example/article",
            "rank": 3,
            "temporal_assessment": _assessment("eligible"),
        },
    ]
    labels = [_label("cand-a"), _label("cand-b"), _label("cand-c")]

    selected = select_candidates(candidates, labels, max_selected=2)

    assert [item["candidate_id"] for item in selected.selected_candidates] == [
        "cand-a",
        "cand-c",
    ]
    duplicate = next(
        item for item in selected.decisions if item.candidate_id == "cand-b"
    )
    assert duplicate.selected is False
    assert "canonical duplicate" in duplicate.reason


def test_open_question_gap_changes_only_deterministic_score() -> None:
    spec = _spec()
    question_id = str(spec.questions[0].question_id)
    candidates = [
        {
            "candidate_id": "cand-targeted",
            "canonical_url": "https://a.example/article",
            "rank": 2,
            "temporal_assessment": _assessment("eligible"),
        },
        {
            "candidate_id": "cand-untargeted",
            "canonical_url": "https://b.example/article",
            "rank": 1,
            "temporal_assessment": _assessment("eligible"),
        },
    ]
    targeted = _label("cand-targeted")
    targeted["target_question_ids"] = [question_id]
    labels = [targeted, _label("cand-untargeted")]

    with_gap = select_candidates(
        candidates,
        labels,
        max_selected=1,
        coverage_gap_question_ids=[question_id],
    )
    without_gap = select_candidates(candidates, labels, max_selected=1)

    with_scores = {
        item.candidate_id: item.deterministic_score for item in with_gap.decisions
    }
    without_scores = {
        item.candidate_id: item.deterministic_score for item in without_gap.decisions
    }
    assert with_scores["cand-targeted"] == without_scores["cand-targeted"] + 2
    assert with_scores["cand-untargeted"] == without_scores["cand-untargeted"]
