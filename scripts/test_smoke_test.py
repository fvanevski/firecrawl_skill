from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import smoke_test
from research_domain.models import RecommendationOutcome
from research_store.orchestrator import OrchestratorConfig, ResearchOrchestrator
from research_store.release_benchmark import MetricStatus


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

    assert smoke_test.gate_passes(result_a, result_b, comparison)
    result_b.recommendation.outcome = "GO"
    assert not smoke_test.gate_passes(result_a, result_b, comparison)
    result_b.recommendation.outcome = RecommendationOutcome.GO.value
    comparison.all_within_tolerance = False
    assert not smoke_test.gate_passes(result_a, result_b, comparison)


def test_longest_text_finds_nested_report_body():
    artifact = {"metadata": {"short": "x"}, "report": {"body": "z" * 250}}

    assert smoke_test.RunEvidenceInspector._longest_text(artifact) == "z" * 250


def test_orchestrator_propagates_supplier_to_semantic_stages():
    supplier = object()
    orchestrator = ResearchOrchestrator(
        run_service=object(),
        coverage_service=object(),
        strategy_service=object(),
        acquisition_service=object(),
        config=object(),
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
