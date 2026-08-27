"""Independent-review regressions for issue #311 deterministic query authority."""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.budget_policy import (
    conservative_research_spec,
)
from firecrawl_skill.research_store.query_policy import (
    materialize_query_plan,
    validate_query_proposal_payload,
)
from firecrawl_skill.research_store.smart_objective_intent import (
    unbounded_discovery_window,
)

ROOT = Path(__file__).resolve().parents[2]
RESEARCH_WORKFLOW = ROOT / "scripts" / "research_workflow.py"


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


def _load_script(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


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
        (
            "expected_organizations",
            [42],
            "expected_organizations values must be strings",
        ),
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


def test_controller_run_rejects_removed_explicit_spec_cli_bypass() -> None:
    from firecrawl_skill.research_store.research_controller_cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--research-spec", "manual.json", "different topic"])


def test_material_scope_change_uses_public_operator_action() -> None:
    from firecrawl_skill.research_store.research_controller_cli import build_parser

    parser = build_parser()
    action_id = "oa_" + "a" * 32
    parsed = parser.parse_args(
        [
            "fork",
            action_id,
            "revised authoritative objective",
            "--reason",
            "material scope change",
            "--authorized-by",
            "human",
        ]
    )
    assert parsed.command == "fork"
    assert parsed.action_id == action_id
    assert "run_id" not in vars(parsed)


def test_legacy_triage_batch_bound_is_invariant_to_candidate_input_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _load_script("issue311_review_workflow", RESEARCH_WORKFLOW)

    def fake_structured(*args, **kwargs):
        del kwargs
        cards = json.loads(args[3].split("Candidate cards:\n", 1)[1])
        return SimpleNamespace(
            value={
                "schema_version": "candidate-semantic-labels-v1",
                "labels": [
                    {
                        "candidate_id": card["candidate_id"],
                        "relevance": "high",
                        "source_suitability": "primary",
                        "target_question_ids": [],
                        "evidence_role": "direct",
                        "rationale": "semantic-only regression label",
                    }
                    for card in cards
                ],
            },
            provenance={},
            attempts=1,
            error=None,
            semantic_call_id=None,
        )

    monkeypatch.setattr(workflow, "_structured", fake_structured)
    base = [
        {"url": "https://c.example/c", "rank": 3, "title": "C"},
        {"url": "https://a.example/a", "rank": 1, "title": "A"},
        {"url": "https://b.example/b", "rank": 2, "title": "B"},
    ]

    first, _ = workflow.triage_candidates(
        "stable triage",
        workflow.conservative_brief("stable triage"),
        deepcopy(base),
        max_candidates_per_batch=2,
        max_batches=1,
    )
    second, _ = workflow.triage_candidates(
        "stable triage",
        workflow.conservative_brief("stable triage"),
        list(reversed(deepcopy(base))),
        max_candidates_per_batch=2,
        max_batches=1,
    )

    assert [item["url"] for item in first] == [item["url"] for item in second]
    assert {item["url"] for item in first} == {
        "https://a.example/a",
        "https://b.example/b",
    }
