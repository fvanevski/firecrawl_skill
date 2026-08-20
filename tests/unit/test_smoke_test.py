from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import smoke_test

from firecrawl_skill.research_domain.models import RecommendationOutcome
from firecrawl_skill.research_store.acquisition.service import AcquisitionService
from firecrawl_skill.research_store.assessment.coverage import CoverageService
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.orchestrator import (
    OrchestratorConfig,
    ResearchOrchestrator,
)
from firecrawl_skill.research_store.release.benchmark import (
    MetricStatus,
    ReleaseBenchmarkResult,
    ReproducibilityComparison,
)
from firecrawl_skill.research_store.run_service import ResearchRunService
from firecrawl_skill.research_store.strategy_service import StrategyRevisionService


def test_verify_candidate_checkout_requires_exact_clean_head(monkeypatch):
    values = iter(["a" * 40, "", "c" * 40])
    monkeypatch.setattr(smoke_test, "_run_git", lambda *args, **kwargs: next(values))

    result = smoke_test.verify_candidate_checkout("a" * 40)

    assert result == {
        "candidate_sha": "a" * 40,
        "tree_hash": "c" * 40,
        "working_tree_clean": True,
    }


def test_verify_candidate_checkout_rejects_mismatch(monkeypatch):
    monkeypatch.setattr(smoke_test, "_run_git", lambda *args, **kwargs: "b" * 40)

    with pytest.raises(smoke_test.SmokeGateError, match="does not match HEAD"):
        smoke_test.verify_candidate_checkout("a" * 40)


def test_verify_candidate_checkout_rejects_dirty_tree(monkeypatch):
    values = iter(["a" * 40, " M scripts/smoke_test.py"])
    monkeypatch.setattr(smoke_test, "_run_git", lambda *args, **kwargs: next(values))

    with pytest.raises(smoke_test.SmokeGateError, match="clean checkout"):
        smoke_test.verify_candidate_checkout("a" * 40)


def test_external_supplier_rejects_autonomous_endpoint():
    with pytest.raises(smoke_test.SmokeGateError, match="must differ"):
        smoke_test.ExternalProcessHostArtifactSupplier(
            ["external-agent"],
            supplier_identity="review-agent",
            source_endpoint="http://localhost:8002/v1/",
            autonomous_endpoints=["http://localhost:8002/v1"],
        )


def test_external_supplier_records_strong_provenance(monkeypatch):
    responses = iter(
        [
            {
                "status": "available",
                "supplier_identity": "review-agent",
                "source_endpoint": "http://review-agent:9000",
            },
            {
                "value": {"answer": "supported"},
                "provenance": {"external_call_id": "call-1"},
                "attempts": [{"attempt": 1}],
            },
        ]
    )

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(next(responses)),
            stderr="",
        )

    monkeypatch.setattr(smoke_test.subprocess, "run", fake_run)
    supplier = smoke_test.ExternalProcessHostArtifactSupplier(
        ["external-agent", "--stdio"],
        supplier_identity="review-agent",
        source_endpoint="http://review-agent:9000",
        autonomous_endpoints=["http://local-model:8002/v1"],
    )

    assert supplier.probe()["status"] == "available"
    result = supplier.supply(
        semantic_context={"schema_name": "answer", "schema_version": 1},
        schema={"type": "object"},
        provider="local",
        model="chat",
        system_prompt="system",
        user_prompt="user",
        prompt_version="v1",
    )

    assert result.error == ""
    assert result.value == {"answer": "supported"}
    assert result.provenance["authority_origin"] == "external-process"
    assert result.provenance["supplier_identity"] == "review-agent"
    assert result.provenance["artifact_sha256"]
    assert result.provenance["request_sha256"]
    assert result.provenance["command_sha256"]
    assert result.attempts == ({"attempt": 1},)


def _metric(name: str, status: MetricStatus, value=1.0):
    return SimpleNamespace(name=name, status=status, value=value)


def test_metric_completeness_rejects_missing_and_unavailable():
    run = SimpleNamespace(
        quality_metrics=tuple(
            _metric(name, MetricStatus.MEASURED)
            for name in smoke_test.MANDATORY_QUALITY_METRICS
            if name != "citation_accuracy"
        ),
        performance_metrics=tuple(
            _metric(name, MetricStatus.UNAVAILABLE, None)
            if name == "total_tokens"
            else _metric(name, MetricStatus.MEASURED)
            for name in smoke_test.MANDATORY_PERFORMANCE_METRICS
        ),
    )

    errors = smoke_test.validate_metric_completeness(run)

    assert "missing mandatory quality metric: citation_accuracy" in errors
    assert "performance metric total_tokens is unavailable" in errors


def test_gate_requires_lowercase_go_and_production_reproducibility():
    result_a = SimpleNamespace(
        recommendation=SimpleNamespace(outcome=RecommendationOutcome.GO.value)
    )
    result_b = SimpleNamespace(
        recommendation=SimpleNamespace(outcome=RecommendationOutcome.GO.value)
    )
    comparison = SimpleNamespace(all_within_tolerance=True)

    assert smoke_test.gate_passes(
        cast(ReleaseBenchmarkResult, result_a),
        cast(ReleaseBenchmarkResult, result_b),
        cast(ReproducibilityComparison, comparison),
    )
    result_b.recommendation.outcome = "GO"
    assert not smoke_test.gate_passes(
        cast(ReleaseBenchmarkResult, result_a),
        cast(ReleaseBenchmarkResult, result_b),
        cast(ReproducibilityComparison, comparison),
    )
    result_b.recommendation.outcome = RecommendationOutcome.GO.value
    comparison.all_within_tolerance = False
    assert not smoke_test.gate_passes(
        cast(ReleaseBenchmarkResult, result_a),
        cast(ReleaseBenchmarkResult, result_b),
        cast(ReproducibilityComparison, comparison),
    )


def test_longest_text_finds_nested_report_body():
    artifact = {"metadata": {"short": "x"}, "report": {"body": "z" * 250}}

    assert smoke_test.RunEvidenceInspector._longest_text(artifact) == "z" * 250


def test_run_evidence_inspector_uses_current_semantic_calls_schema():
    queries = (
        smoke_test.RunEvidenceInspector._COUNT_QUERIES["semantic_calls"],
        smoke_test.RunEvidenceInspector._AUTHORITY_COUNT_QUERY,
        smoke_test.RunEvidenceInspector._HOST_METADATA_QUERY,
    )
    combined = "\n".join(queries)

    assert all("status='complete'" in query for query in queries)
    assert "request->>'authority'" in queries[1]
    assert "request->>'authority'" in queries[2]
    assert "call_status" not in combined
    assert "SELECT semantic_authority" not in combined
    assert "AND semantic_authority=" not in combined


def test_orchestrator_propagates_supplier_to_semantic_stages():
    supplier = object()
    orchestrator = ResearchOrchestrator(
        run_service=cast(ResearchRunService, object()),
        coverage_service=cast(CoverageService, object()),
        strategy_service=cast(StrategyRevisionService, object()),
        acquisition_service=cast(AcquisitionService, object()),
        config=cast(StoreConfig, object()),
        corpus_service=object(),
        evidence_service=object(),
        orchestrator_config=OrchestratorConfig(host_artifact_supplier=supplier),
    )

    assert orchestrator._evidence_preparation.host_artifact_supplier is supplier
    assert orchestrator._synthesis.host_artifact_supplier is supplier


def test_external_supplier_requires_autonomous_endpoint_fingerprint():
    with pytest.raises(smoke_test.SmokeGateError, match="endpoint fingerprint"):
        smoke_test.ExternalProcessHostArtifactSupplier(
            ["external-agent"],
            supplier_identity="review-agent",
            source_endpoint="http://review-agent:9000",
            autonomous_endpoints=[],
        )


def test_draft_report_text_uses_only_completed_draft_sections():
    rows = [
        ("outline", "completed", {"long_prompt": "x" * 500}),
        ("draft", "failed", {"report_sections": [{"body": "y" * 500}]}),
        (
            "draft",
            "completed",
            {"report_sections": [{"body": "first"}, {"body": "second"}]},
        ),
        ("citation_pass", "completed", {"analysis": "z" * 500}),
    ]

    assert smoke_test.RunEvidenceInspector._draft_report_text(rows) == (
        "first\n\nsecond"
    )


def test_execution_modes_default_to_autonomous_and_deterministic():
    modes, disabled = smoke_test.resolve_execution_modes(
        include_agent_led=False, environ={}
    )

    assert modes == ("autonomous_local", "deterministic_debug")
    assert disabled is False


def test_include_agent_led_selects_all_modes():
    modes, disabled = smoke_test.resolve_execution_modes(
        include_agent_led=True, environ={}
    )

    assert modes == smoke_test.RELEASE_MODES
    assert disabled is False


def test_disable_agent_led_environment_override_wins():
    modes, disabled = smoke_test.resolve_execution_modes(
        include_agent_led=True, environ={"SMOKE_DISABLE_AGENT_LED": "yes"}
    )

    assert modes == smoke_test.DEFAULT_SMOKE_MODES
    assert disabled is True


def test_false_disable_override_allows_include_flag():
    modes, disabled = smoke_test.resolve_execution_modes(
        include_agent_led=True, environ={"SMOKE_DISABLE_AGENT_LED": "0"}
    )

    assert modes == smoke_test.RELEASE_MODES
    assert disabled is False


def test_invalid_disable_override_fails_closed():
    with pytest.raises(smoke_test.SmokeGateError, match="SMOKE_DISABLE_AGENT_LED"):
        smoke_test.resolve_execution_modes(
            include_agent_led=True,
            environ={"SMOKE_DISABLE_AGENT_LED": "sometimes"},
        )


def test_parser_agent_led_is_opt_in():
    parser = smoke_test.build_parser()
    defaults = parser.parse_args(["--candidate-sha", "a" * 40])
    enabled = parser.parse_args(["--candidate-sha", "a" * 40, "--include-agent-led"])

    assert defaults.include_agent_led is False
    assert enabled.include_agent_led is True


def test_model_gateway_retry_keeps_schema_constraint(monkeypatch):
    from firecrawl_skill import model_gateway

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"status": {"type": "string", "enum": ["ok"]}},
        "required": ["status"],
        "additionalProperties": False,
    }
    schema_echo = {
        "$schema": schema["$schema"],
        "description": "This is the schema, not an instance.",
        "properties": schema["properties"],
        "additionalProperties": False,
    }
    responses = iter(
        [
            (
                {
                    "id": "attempt-1",
                    "model": "chat",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(schema_echo)},
                        }
                    ],
                    "usage": {},
                },
                "request-1",
                200,
            ),
            (
                {
                    "id": "attempt-2",
                    "model": "chat",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps({"status": "ok"})},
                        }
                    ],
                    "usage": {},
                },
                "request-2",
                200,
            ),
        ]
    )
    payloads = []

    def fake_request(_url, payload, _headers, _timeout):
        payloads.append(payload)
        return next(responses)

    monkeypatch.setattr(model_gateway, "_request_json", fake_request)
    monkeypatch.setattr(
        model_gateway,
        "probe_local",
        lambda *_args, **_kwargs: {"status": "available"},
    )

    result = model_gateway.call_structured(
        provider="local",
        model="chat",
        system_prompt="Return the required status object.",
        user_prompt="Produce the result.",
        schema=schema,
        max_attempts=2,
    )

    assert result.error == ""
    assert result.value == {"status": "ok"}
    assert [item["response_format"]["type"] for item in payloads] == [
        "json_schema",
        "json_schema",
    ]
    repair_prompt = payloads[1]["messages"][1]["content"]
    assert "Return only a JSON instance" in repair_prompt
    assert "matching this exact schema" not in repair_prompt
    assert '"properties":' not in repair_prompt
    assert '"additionalProperties": false' not in repair_prompt


def test_local_model_call_persists_policy_authority():
    from uuid import uuid4

    from firecrawl_skill.research_store.execution_policy import SemanticAuthority
    from firecrawl_skill.research_store.semantic_service import SemanticCallService

    class Runs:
        request = None

        def get_run_status(self, *, run_id):
            return {
                "lifecycle_revision": 1,
                "execution_mode": "autonomous_local",
            }

        def record_semantic_call(
            self,
            run_id,
            stage,
            provider,
            model,
            prompt_version,
            request,
            idempotency_key,
            **kwargs,
        ):
            self.request = request
            return uuid4()

    class UnitOfWork:
        def __init__(self, runs):
            self.runs = runs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    runs = Runs()
    service = SemanticCallService(lambda: UnitOfWork(runs))
    run_id = uuid4()

    service.start_model_call(
        {
            "run_id": str(run_id),
            "run_revision": 1,
            "stage": "draft",
            "schema_name": "synthesis-draft-v1",
            "schema_version": 1,
            "idempotency_key": "draft-call",
        },
        provider="local",
        requested_model="chat",
        model_revision="",
        endpoint_alias="local",
        prompt_version="v1",
        prompt_hash="abc",
        schema={"type": "object"},
        input_token_estimate=1,
    )

    assert runs.request is not None
    assert runs.request["authority"] == SemanticAuthority.LOCAL_MODEL.value
