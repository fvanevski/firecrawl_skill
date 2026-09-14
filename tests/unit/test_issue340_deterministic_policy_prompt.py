"""Issue #340 semantic query prompt/validator contract regressions."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from firecrawl_skill.research_domain import serialize_model
from firecrawl_skill.research_store import (
    authorized_semantic as authorized_semantic_module,
)
from firecrawl_skill.research_store import query_policy as query_policy_module
from firecrawl_skill.research_store.budget_policy import conservative_research_spec
from firecrawl_skill.research_store.execution_policy import ExecutionModeError
from firecrawl_skill.research_store.query_policy import (
    QUERY_PROPOSAL_SCHEMA,
    parse_query_structure,
    semantic_query_proposals,
)
from firecrawl_skill.research_store.semantic_service import SemanticCallService
from firecrawl_skill.research_store.smart_search_application import plan_queries


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

    def fake_call_local_structured(**kwargs: Any) -> SimpleNamespace:
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
        "call_local_structured",
        fake_call_local_structured,
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


def test_query_planning_bypasses_agent_led_host_supplier_for_local_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    payload: dict[str, Any] = {
        "schema_version": "search-query-proposal-v1",
        "queries": [_proposal(spec, "local planner evidence")],
    }
    host_calls: list[dict[str, Any]] = []
    gateway_calls: list[dict[str, Any]] = []
    persisted_calls: list[dict[str, Any]] = []

    class _HostSupplier:
        def supply(self, **kwargs: Any) -> None:
            host_calls.append(kwargs)
            raise AssertionError("query planning must not delegate to host authority")

    class _SemanticCalls:
        def record_semantic_call(self, *args: Any, **kwargs: Any):
            persisted_calls.append({"args": args, "kwargs": kwargs})
            return uuid4()

    class _AgentLedUow:
        runs = SimpleNamespace(
            get_run_status=lambda *, run_id: {
                "execution_mode": "agent_led",
                "lifecycle_revision": 1,
            }
        )
        semantic_calls = _SemanticCalls()

        def __enter__(self):
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    service = SemanticCallService(
        lambda: _AgentLedUow(),
        host_artifact_supplier=_HostSupplier(),
    )

    def fake_gateway_call(**kwargs: Any) -> SimpleNamespace:
        gateway_calls.append(kwargs)
        call_id = kwargs["semantic_persistence"].start_model_call(
            kwargs["semantic_context"],
            provider=kwargs["provider"],
            requested_model="chat",
            model_revision="",
            endpoint_alias="local",
            prompt_version=kwargs["prompt_version"],
            prompt_hash="test-prompt-hash",
            schema=kwargs["schema"],
            input_token_estimate=1,
        )
        kwargs["post_validate"](payload)
        return SimpleNamespace(
            value=payload,
            error=None,
            provenance={"provider": "local"},
            semantic_call_id=call_id,
            artifact_ids=(),
            attempts=(),
        )

    monkeypatch.setattr(
        authorized_semantic_module.model_gateway,
        "call_structured",
        fake_gateway_call,
    )

    queries, provenance = semantic_query_proposals(
        topic=spec.objective,
        max_queries=1,
        semantic_service=service,
        semantic_context={
            "run_id": str(uuid4()),
            "run_revision": 1,
            "stage": "planning",
            "schema_name": "search-query-proposal-v1",
            "schema_version": 1,
            "artifact_type": "search_query_proposal",
            "idempotency_key": "agent-led-local-planner-regression",
        },
        spec=spec,
    )

    assert queries == payload["queries"]
    assert provenance["status"] == "succeeded"
    assert gateway_calls
    assert gateway_calls[0]["provider"] == "local"
    assert gateway_calls[0]["semantic_context"]["semantic_stage_authority"] == (
        "local-query-planner-v1"
    )
    assert persisted_calls
    assert persisted_calls[0]["kwargs"]["expected_execution_mode"] == "agent_led"
    assert persisted_calls[0]["args"][5]["semantic_stage_authority"] == (
        "local-query-planner-v1"
    )
    assert host_calls == []


def test_agent_led_generic_model_persistence_cannot_forge_planner_context() -> None:
    class _SemanticCalls:
        def record_semantic_call(self, *_args: Any, **_kwargs: Any):
            raise AssertionError("forged generic call must not persist")

    class _AgentLedUow:
        runs = SimpleNamespace(
            get_run_status=lambda *, run_id: {
                "execution_mode": "agent_led",
                "lifecycle_revision": 1,
            }
        )
        semantic_calls = _SemanticCalls()

        def __enter__(self):
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    service = SemanticCallService(lambda: _AgentLedUow())
    with pytest.raises(ExecutionModeError):
        service.start_model_call(
            {
                "run_id": str(uuid4()),
                "run_revision": 1,
                "stage": "planning",
                "schema_name": "search-query-proposal-v1",
                "schema_version": 1,
                "artifact_type": "search_query_proposal",
                "semantic_stage_authority": "local-query-planner-v1",
                "idempotency_key": "forged-planner-context",
            },
            provider="local",
            requested_model="chat",
            model_revision="",
            endpoint_alias="local",
            prompt_version="search-query-proposal-v1",
            prompt_hash="forged",
            schema=QUERY_PROPOSAL_SCHEMA,
            input_token_estimate=1,
        )


def test_local_query_planner_rejects_nonlocal_provider_before_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Uow:
        runs = SimpleNamespace(
            get_run_status=lambda *, run_id: {
                "execution_mode": "agent_led",
                "lifecycle_revision": 1,
            }
        )

        def __enter__(self):
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    service = SemanticCallService(lambda: _Uow())
    monkeypatch.setattr(
        authorized_semantic_module.model_gateway,
        "call_structured",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("nonlocal planner call reached the gateway")
        ),
    )

    with pytest.raises(ExecutionModeError, match="provider='local'"):
        authorized_semantic_module.call_local_structured(
            semantic_service=service,
            semantic_context={
                "run_id": str(uuid4()),
                "run_revision": 1,
                "stage": "planning",
                "schema_name": "search-query-proposal-v1",
                "schema_version": 1,
                "artifact_type": "search_query_proposal",
                "idempotency_key": "nonlocal-planner-provider",
            },
            deterministic_fixture={"schema_version": "search-query-proposal-v1", "queries": []},
            actor_identifier="test",
            provider="openai",
            model="gpt-test",
            schema=QUERY_PROPOSAL_SCHEMA,
            system_prompt="test",
            user_prompt="test",
            prompt_version="search-query-proposal-v1",
        )


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

    def fake_call_local_structured(**kwargs: Any) -> SimpleNamespace:
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
        "call_local_structured",
        fake_call_local_structured,
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
        )
