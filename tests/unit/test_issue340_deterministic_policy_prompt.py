"""Issue #340 semantic query prompt/validator contract regressions."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from firecrawl_skill.research_domain import serialize_model
from firecrawl_skill.research_store import query_policy as query_policy_module
from firecrawl_skill.research_store.budget_policy import conservative_research_spec
from firecrawl_skill.research_store.query_policy import (
    parse_query_structure,
    semantic_query_proposals,
)
from firecrawl_skill.research_store.semantic_service import SemanticCallService
from firecrawl_skill.research_store.smart_search_application import (
    local_semantic_query_planner,
    plan_queries,
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


def _semantic_service() -> SemanticCallService:
    return cast(
        SemanticCallService,
        SimpleNamespace(host_artifact_supplier=None),
    )


def test_semantic_query_prompt_contract_matches_hostname_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    payload: dict[str, Any] = {
        "schema_version": "search-query-proposal-v1",
        "queries": [_proposal(spec, "evidence site:github.com -site:example.com")],
    }
    captured: dict[str, str] = {}

    def fake_call_authorized_structured(**kwargs: Any) -> SimpleNamespace:
        captured["system_prompt"] = kwargs["system_prompt"]
        kwargs["post_validate"](payload)
        return SimpleNamespace(
            value=payload,
            error=None,
            provenance={},
            semantic_call_id=None,
            artifact_ids=(),
        )

    monkeypatch.setattr(
        query_policy_module,
        "call_authorized_structured",
        fake_call_authorized_structured,
    )

    queries, provenance = semantic_query_proposals(
        topic=spec.objective,
        max_queries=1,
        semantic_service=_semantic_service(),
        semantic_context={},
        spec=spec,
    )

    prompt = captured["system_prompt"]
    assert "bare domain/hostname only" in prompt
    assert "path, query, and fragment components are prohibited" in prompt
    assert "Valid examples: site:github.com and -site:example.com." in prompt
    assert "Invalid example: site:github.com/org/repo." in prompt
    assert queries == payload["queries"]
    assert provenance["status"] == "succeeded"

    positive = parse_query_structure("evidence site:github.com")
    negative = parse_query_structure("evidence -site:example.com")
    assert positive["domain_restrictions"] == ["github.com"]
    assert negative["negative_terms"] == ["site:example.com"]


@pytest.mark.parametrize(
    "operand",
    [
        "site:github.com/",
        "site:https://github.com",
        "site:github.com:443",
        "site:user@github.com",
        "site:github.com/org/repo",
        "site:github.com?tab=readme",
        "site:github.com#readme",
    ],
)
def test_non_bare_site_operands_fail_closed(operand: str) -> None:
    with pytest.raises(ValueError, match="bare domain/hostname"):
        parse_query_structure(f"evidence {operand}")


@pytest.mark.parametrize(
    "operand",
    ["site:github.com/org/repo", "site:https://github.com"],
)
def test_non_bare_site_validation_failure_fails_closed_without_planner_fallback(
    monkeypatch: pytest.MonkeyPatch,
    operand: str,
) -> None:
    spec = _spec()
    invalid_payload: dict[str, Any] = {
        "schema_version": "search-query-proposal-v1",
        "queries": [_proposal(spec, f"evidence {operand}")],
    }

    def fake_call_authorized_structured(**kwargs: Any) -> SimpleNamespace:
        try:
            kwargs["post_validate"](invalid_payload)
        except ValueError as exc:
            error = str(exc)
        else:
            raise AssertionError("non-bare site: proposal unexpectedly validated")
        return SimpleNamespace(
            value=None,
            error=error,
            provenance={},
            semantic_call_id=None,
            artifact_ids=(),
        )

    monkeypatch.setattr(
        query_policy_module,
        "call_authorized_structured",
        fake_call_authorized_structured,
    )

    with pytest.raises(
        ValueError,
        match="local semantic query planner produced no authorized queries",
    ):
        plan_queries(
            spec.objective,
            1,
            _semantic_service(),
            {"research_spec": serialize_model(spec)},
            local_semantic_query_planner,
        )
