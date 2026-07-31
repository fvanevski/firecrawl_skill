"""Production-boundary regression tests for strict release invariants.

These tests are intentionally fast and database-free.  They invoke the real
authority router, metric-status engine, recommendation policy, strict CLI,
canonical matcher, and complete-preflight orchestrator.  PostgreSQL-specific
citation and cache-isolation regressions live in their integration suites.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest import mock
from uuid import UUID, uuid4

import pytest
from research_domain.models import (
    BenchmarkDataset,
    BenchmarkObjective,
    BenchmarkSource,
    PerformanceMeasurement,
    QualityMeasurement,
    ReleaseRecommendation,
    WorkflowComparison,
    WorkflowRunResult,
)
from research_store.authorized_semantic import call_authorized_structured
from research_store.execution_policy import ExecutionModeError
from research_store.release_benchmark import (
    MANDATORY_PERFORMANCE_METRICS,
    MANDATORY_QUALITY_METRICS,
    CampaignRun,
    MetricEngine,
    MetricSource,
    MetricStatus,
    PerformanceMetric,
    QualityMetric,
    ReleaseBenchmarkConfig,
    ReleaseBenchmarkResult,
    ReleaseBenchmarkRunner,
    ReproducibilityComparison,
    _canonical_match,
)
from research_store.semantic_service import HostArtifactResult
from research_store.strict_benchmark import _preflight_check, main

SCRIPTS = Path(__file__).resolve().parent
BENCHMARK_FIXTURE = (
    SCRIPTS.parent / "tests" / "fixtures" / "benchmark" / "benchmark-v1.json"
)


class _Cursor:
    def __init__(self, row=None, rows=()):
        self._row = row
        self._rows = tuple(rows)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args, **_kwargs):
        return self

    def fetchone(self):
        return self._row

    def fetchall(self):
        return list(self._rows)


class _MetricConnection:
    def __init__(
        self,
        *,
        semantic_calls: int = 2,
        cache_event_ids: tuple[str, ...] = ("cache-event-1", "cache-event-2"),
        cache_stages: tuple[str, ...] = ("draft",),
    ):
        self.semantic_calls = semantic_calls
        self.cache_event_ids = cache_event_ids
        self.cache_stages = cache_stages

    def cursor(self):
        return _Cursor((self.semantic_calls,))

    def execute(self, *_args, **_kwargs):
        return _Cursor((list(self.cache_event_ids), list(self.cache_stages)))

    def rollback(self):
        return None


def _dataset() -> BenchmarkDataset:
    objective = BenchmarkObjective(
        schema_version="benchmark-objective-v1",
        id="obj-001",
        title="Release invariant",
        objective="Verify strict release policy.",
        questions=("Does the release path fail closed?",),
        expected_source_classes=("official",),
        known_relevant_sources=(
            BenchmarkSource(
                schema_version="benchmark-source-v1",
                file_path="scripts/research_store/release_benchmark.py",
                relevance=True,
                role="relevant",
            ),
        ),
        known_distractor_sources=(),
        expected_unresolved_controversies=(),
        citation_support_labels={"q-1": "SUPPORTED"},
    )
    return BenchmarkDataset(
        schema_version="benchmark-dataset-v1",
        version="release-invariants-v1",
        description="Strict release invariant regression fixture.",
        evaluation_set=True,
        objectives=(objective,),
        quality_thresholds={
            "min_candidate_recall": 0.5,
            "min_source_quality_score": 0.7,
            "min_coverage_completeness": 0.5,
            "max_unsupported_claim_rate": 0.15,
            "min_citation_accuracy": 0.8,
        },
        workflow_modes=("agent_led", "autonomous_local", "deterministic_debug"),
        deterministic_integrity_checks=(),
    )


def _quality() -> QualityMeasurement:
    return QualityMeasurement(
        schema_version="quality-measurement-v3",
        candidate_recall=0.9,
        source_quality_score=0.9,
        coverage_completeness=0.9,
        unsupported_claim_rate=0.0,
        citation_accuracy=0.95,
        report_quality_score=0.9,
    )


def _performance() -> PerformanceMeasurement:
    return PerformanceMeasurement(
        schema_version="performance-measurement-v2",
        total_latency_ms=100.0,
        total_tokens=100,
        semantic_calls=2,
        cache_hit_rate=0.5,
        embedding_throughput=10.0,
        gpu_memory_mb=256.0,
        cpu_percent=20.0,
    )


def _quality_metrics(run_id: UUID) -> tuple[QualityMetric, ...]:
    values = {
        "candidate_recall": 0.9,
        "source_quality_score": 0.9,
        "coverage_completeness": 0.9,
        "unsupported_claim_rate": 0.0,
        "citation_accuracy": 0.95,
        "report_quality_score": 0.9,
    }
    return tuple(
        QualityMetric(
            name=name,
            value=values[name],
            source=MetricSource(
                table="test_quality",
                column=name,
                run_id=str(run_id),
                method="regression_fixture",
            ),
            formula="authoritative regression fixture",
            status=MetricStatus.MEASURED,
        )
        for name in sorted(MANDATORY_QUALITY_METRICS)
    )


def _performance_metrics(run_id: UUID) -> tuple[PerformanceMetric, ...]:
    values = {
        "total_tokens": 100.0,
        "cache_hit_rate": 0.5,
        "embedding_throughput": 10.0,
        "cpu_percent": 20.0,
        "gpu_memory_mb": 256.0,
    }
    return tuple(
        PerformanceMetric(
            name=name,
            value=values[name],
            source=MetricSource(
                table="test_performance",
                column=name,
                run_id=str(run_id),
                method="regression_fixture",
            ),
            formula="authoritative regression fixture",
            status=MetricStatus.MEASURED,
        )
        for name in sorted(MANDATORY_PERFORMANCE_METRICS)
    )


def _recommend(
    *,
    mode: str,
    performance: PerformanceMeasurement,
    performance_metrics: tuple[PerformanceMetric, ...],
    errors: tuple[str, ...] = (),
) -> ReleaseRecommendation:
    run_id = uuid4()
    target = WorkflowRunResult(
        schema_version="workflow-run-result-v1",
        workflow_mode=mode,
        quality=_quality(),
        performance=performance,
        integrity_checks=(),
        run_id=run_id,
        errors=errors,
        quality_metrics=_quality_metrics(run_id),
        performance_metrics=performance_metrics,
    )
    baseline_mode = "autonomous_local" if mode != "autonomous_local" else "agent_led"
    baseline_id = uuid4()
    baseline = WorkflowRunResult(
        schema_version="workflow-run-result-v1",
        workflow_mode=baseline_mode,
        quality=_quality(),
        performance=_performance(),
        integrity_checks=(),
        run_id=baseline_id,
        errors=(),
        quality_metrics=_quality_metrics(baseline_id),
        performance_metrics=_performance_metrics(baseline_id),
    )
    comparison = WorkflowComparison(
        schema_version="workflow-comparison-v1",
        dataset_version="release-invariants-v1",
        results=(target, baseline),
        quality_vs_baseline={mode: 1.0, baseline_mode: 1.0},
        performance_vs_baseline={mode: 1.0, baseline_mode: 1.0},
        integrity_regression=False,
    )
    runner = ReleaseBenchmarkRunner(
        _dataset(),
        ReleaseBenchmarkConfig(
            execution_modes=(mode, baseline_mode),
            strict=True,
            host_artifact_supplier=object(),
        ),
    )
    return runner._build_recommendation(comparison)


def _telemetry(**overrides):
    values = {
        "total_tokens": 100,
        "token_source": "endpoint",
        "cache_lookups": 2,
        "cache_hits": 1,
        "cache_misses": 1,
        "embedding_batch_count": 1,
        "embedding_throughput": 10.0,
        "embedding_total_texts": 10,
        "embedding_elapsed_seconds": 1.0,
        "cpu_samples": 1,
        "cpu_mean_percent": 20.0,
        "gpu_samples": 1,
        "gpu_mean_memory_mb": 256.0,
        "telemetry_tables_exist": True,
    }
    values.update(overrides)
    return values


def _token_completeness(**overrides):
    values = {"semantic_calls": 2, "usage_records": 2, "uncovered_calls": 0}
    values.update(overrides)
    return values


def _embedding_completeness(**overrides):
    values = {
        "batch_count": 1,
        "vector_count": 10,
        "failed_count": 0,
        "total_texts": 10,
        "elapsed_seconds": 1.0,
        "text_vector_mismatch": False,
    }
    values.update(overrides)
    return values


def _resource_completeness(**overrides):
    values = {
        "total_count": 1,
        "measured_count": 1,
        "unavailable_count": 0,
        "invalid_count": 0,
        "partial_count": 0,
        "stale_count": 0,
        "missing_window": 0,
        "has_failure_reason": 0,
    }
    values.update(overrides)
    return values


def _resource_source(device_type: str, *, measured_count: int = 1):
    return {
        "record_ids": (f"{device_type}-sample-1",) if measured_count else (),
        "measured_count": measured_count,
        "total_count": measured_count,
        "invalid_count": 0,
        "device_index": 0 if measured_count else None,
        "device_uuid": "GPU-test" if device_type == "gpu" and measured_count else "",
        "collector": "pynvml" if device_type == "gpu" else "psutil",
        "collector_version": "test",
        "status_counts": (("measured", measured_count),) if measured_count else (),
    }


def _extract_performance(
    monkeypatch: pytest.MonkeyPatch,
    *,
    telemetry=None,
    token=None,
    embedding=None,
    resources=None,
    resource_sources=None,
):
    engine = MetricEngine(
        "postgresql://test",
        config=ReleaseBenchmarkConfig(strict=True),
    )
    engine._connection = _MetricConnection()
    monkeypatch.setattr(
        engine,
        "_read_telemetry",
        lambda _run_id: telemetry or _telemetry(),
    )
    monkeypatch.setattr(
        engine,
        "_check_token_completeness",
        lambda _run_id: token or _token_completeness(),
    )
    monkeypatch.setattr(
        engine,
        "_check_embedding_completeness",
        lambda _run_id: embedding or _embedding_completeness(),
    )
    resources = resources or {
        "cpu": _resource_completeness(),
        "gpu": _resource_completeness(),
    }
    resource_sources = resource_sources or {
        "cpu": _resource_source("cpu"),
        "gpu": _resource_source("gpu"),
    }
    monkeypatch.setattr(
        engine,
        "_check_resource_completeness",
        lambda _run_id, device_type: resources[device_type],
    )
    monkeypatch.setattr(
        engine,
        "_read_resource_source",
        lambda _run_id, device_type: resource_sources[device_type],
    )
    return engine.extract_performance_metrics(uuid4(), time.monotonic() - 0.01)


class _Runs:
    def __init__(self, mode: str):
        self.mode = mode

    def get_run_status(self, *, run_id):
        return {"execution_mode": self.mode}


class _Uow:
    def __init__(self, mode: str):
        self.runs = _Runs(mode)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _SemanticService:
    def __init__(self, mode: str, *, reject_payload: bool = False):
        self.mode = mode
        self.reject_payload = reject_payload
        self.ingested = []

    def uow_factory(self):
        return _Uow(self.mode)

    def ingest_host_artifact(
        self,
        context,
        payload,
        schema,
        *,
        actor_identifier,
    ):
        if self.reject_payload:
            raise ValueError("host artifact schema validation failed")
        self.ingested.append((context, payload, schema, actor_identifier))
        return HostArtifactResult(
            value=payload,
            provenance={},
            attempts=(),
        )


def _authorized_call(service, supplier):
    return call_authorized_structured(
        semantic_service=service,
        semantic_context={"run_id": str(uuid4())},
        deterministic_fixture={"status": "fixture"},
        actor_identifier="test-host",
        host_artifact_supplier=supplier,
        schema={
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
            "additionalProperties": False,
        },
        prompt="Return the structured status.",
        system_prompt="You are a regression probe.",
        model="test-model",
    )


def test_agent_led_without_supplier_hard_fails():
    with pytest.raises(ExecutionModeError, match="HostArtifactSupplier"):
        _authorized_call(_SemanticService("agent_led"), None)


@pytest.mark.parametrize(
    ("generated", "message"),
    [
        (
            HostArtifactResult(None, {}, (), "supplier transport failed"),
            "supplier failed",
        ),
        (
            HostArtifactResult(None, {}, ()),
            "returned no artifact",
        ),
    ],
)
def test_agent_led_failed_or_empty_supplier_hard_fails(generated, message):
    supplier = mock.Mock()
    supplier.supply.return_value = generated
    with pytest.raises(ExecutionModeError, match=message):
        _authorized_call(_SemanticService("agent_led"), supplier)


def test_agent_led_invalid_supplied_artifact_hard_fails():
    supplier = mock.Mock()
    supplier.supply.return_value = HostArtifactResult(
        {"unexpected": True},
        {"usage": {}},
        (),
    )
    with pytest.raises(ValueError, match="schema validation"):
        _authorized_call(
            _SemanticService("agent_led", reject_payload=True),
            supplier,
        )


def test_agent_led_uses_host_authority_and_never_local_model():
    supplier = mock.Mock()
    supplier.supply.return_value = HostArtifactResult(
        {"status": "host-authored"},
        {"usage": {}},
        (),
    )
    service = _SemanticService("agent_led")
    with mock.patch(
        "research_store.authorized_semantic.model_gateway.call_structured",
        side_effect=AssertionError("local-model authority must not run"),
    ) as local_call:
        result = _authorized_call(service, supplier)

    assert result.value == {"status": "host-authored"}
    supplier.supply.assert_called_once()
    local_call.assert_not_called()
    assert service.ingested
    supplied_context = service.ingested[0][0]
    assert supplied_context["supplied_response_metadata"]["provenance"] == {"usage": {}}


def test_not_invoked_tokens_are_not_applicable_and_can_satisfy_release(
    monkeypatch,
):
    performance, metrics = _extract_performance(
        monkeypatch,
        telemetry=_telemetry(total_tokens=0, token_source="not_invoked"),
    )
    token_metric = next(metric for metric in metrics if metric.name == "total_tokens")

    assert token_metric.status == MetricStatus.NOT_APPLICABLE
    assert "did not execute a model" in token_metric.formula
    recommendation = _recommend(
        mode="deterministic_debug",
        performance=performance,
        performance_metrics=metrics,
    )
    assert recommendation.outcome == "go"


def test_missing_semantic_usage_is_incomplete_and_forces_no_go(monkeypatch):
    performance, metrics = _extract_performance(
        monkeypatch,
        token=_token_completeness(
            semantic_calls=3,
            usage_records=2,
            uncovered_calls=1,
        ),
    )
    token_metric = next(metric for metric in metrics if metric.name == "total_tokens")

    assert token_metric.status == MetricStatus.INCOMPLETE
    assert "1 uncovered" in token_metric.formula
    recommendation = _recommend(
        mode="agent_led",
        performance=performance,
        performance_metrics=metrics,
    )
    assert recommendation.outcome == "no_go"


def test_embedding_batch_failure_is_incomplete_and_forces_no_go(monkeypatch):
    performance, metrics = _extract_performance(
        monkeypatch,
        embedding=_embedding_completeness(
            vector_count=9,
            failed_count=1,
            total_texts=10,
            text_vector_mismatch=True,
        ),
    )
    embedding_metric = next(
        metric for metric in metrics if metric.name == "embedding_throughput"
    )

    assert embedding_metric.status == MetricStatus.INCOMPLETE
    assert "failed_count=1" in embedding_metric.formula
    recommendation = _recommend(
        mode="agent_led",
        performance=performance,
        performance_metrics=metrics,
    )
    assert recommendation.outcome == "no_go"


def test_completed_metrics_with_run_errors_force_no_go():
    run_id = uuid4()
    recommendation = _recommend(
        mode="agent_led",
        performance=_performance(),
        performance_metrics=_performance_metrics(run_id),
        errors=("execution failed after metrics completed",),
    )

    assert recommendation.outcome == "no_go"
    assert any("execution errors" in item for item in recommendation.withdrawn_claims)


def test_unavailable_resource_collectors_are_null_with_explicit_reason(monkeypatch):
    performance, metrics = _extract_performance(
        monkeypatch,
        telemetry=_telemetry(
            cpu_samples=0,
            cpu_mean_percent=None,
            gpu_samples=0,
            gpu_mean_memory_mb=None,
        ),
        resources={
            "cpu": _resource_completeness(
                total_count=1,
                measured_count=0,
                unavailable_count=1,
                has_failure_reason=1,
            ),
            "gpu": _resource_completeness(
                total_count=1,
                measured_count=0,
                unavailable_count=1,
                has_failure_reason=1,
            ),
        },
        resource_sources={
            "cpu": _resource_source("cpu", measured_count=0),
            "gpu": _resource_source("gpu", measured_count=0),
        },
    )
    by_name = {metric.name: metric for metric in metrics}

    assert performance.cpu_percent is None
    assert by_name["cpu_percent"].status == MetricStatus.UNAVAILABLE
    assert "no measured process-scoped CPU samples" in by_name["cpu_percent"].formula
    assert performance.gpu_memory_mb is None
    assert by_name["gpu_memory_mb"].status == MetricStatus.UNAVAILABLE
    assert "no measured GPU samples" in by_name["gpu_memory_mb"].formula


def test_partial_resource_window_is_incomplete(monkeypatch):
    performance, metrics = _extract_performance(
        monkeypatch,
        resources={
            "cpu": _resource_completeness(missing_window=1),
            "gpu": _resource_completeness(),
        },
    )
    cpu_metric = next(metric for metric in metrics if metric.name == "cpu_percent")

    assert performance.cpu_percent is None
    assert cpu_metric.status == MetricStatus.INCOMPLETE
    recommendation = _recommend(
        mode="agent_led",
        performance=performance,
        performance_metrics=metrics,
    )
    assert recommendation.outcome == "no_go"


def _campaign_result(*, outcome: str, conditions=()) -> ReleaseBenchmarkResult:
    run = CampaignRun(
        campaign_id="fr_test",
        run_id="fr_run_test",
        mode="agent_led",
        objective_id="obj-001",
        quality=_quality(),
        performance=_performance(),
    )
    recommendation = ReleaseRecommendation(
        schema_version="release-recommendation-v1",
        outcome=outcome,
        dataset_version="release-invariants-v1",
        comparison=None,
        supported_claims=() if outcome != "go" else ("measured",),
        withdrawn_claims=(),
        known_limitations=(),
        conditions=conditions,
        p0_regressions=(),
    )
    return ReleaseBenchmarkResult(
        schema_version="release-benchmark-result-v1",
        campaign_id="fr_test",
        campaign_timestamp="2026-07-30T00:00:00+00:00",
        environment={},
        runs=(run,),
        recommendation=recommendation,
        total_duration_ms=1.0,
    )


def test_go_with_conditions_returns_nonzero_strict_cli_exit(tmp_path):
    result = _campaign_result(
        outcome="go_with_conditions",
        conditions=("operator review required",),
    )
    comparison = ReproducibilityComparison(
        run_a_id="fr_a",
        run_b_id="fr_b",
        all_within_tolerance=True,
        quality_tolerances=(),
        performance_tolerances=(),
        details=(),
    )

    with (
        mock.patch(
            "research_store.strict_benchmark._preflight_check",
            return_value=(True, []),
        ),
        mock.patch(
            "research_store.strict_benchmark._run_campaign",
            side_effect=((result, "hash-a"), (result, "hash-b")),
        ),
        mock.patch(
            "research_store.strict_benchmark._compare_campaigns",
            return_value=comparison,
        ),
        mock.patch(
            "research_store.strict_benchmark._build_manifest",
            return_value={"schema_version": "campaign-manifest-v1"},
        ),
        mock.patch(
            "research_store.strict_benchmark._write_json_atomic",
            return_value="manifest-hash",
        ),
        mock.patch(
            "research_store.strict_benchmark._compute_file_hash",
            return_value="artifact-hash",
        ),
    ):
        rc = main(
            [
                "--candidate-sha",
                "a" * 40,
                "--database-url",
                "postgresql://test",
                "--dataset",
                str(BENCHMARK_FIXTURE),
                "--campaign-dir",
                str(tmp_path),
            ]
        )

    assert rc != 0


def test_basename_collision_does_not_false_match():
    assert (
        _canonical_match(
            "scripts/foo.py",
            "https://example.com/other/foo.py",
        )
        is False
    )
    assert (
        _canonical_match(
            "scripts/foo.py",
            "https://example.com/scripts/foo.py",
        )
        is True
    )


def _successful_probe_patches():
    return (
        mock.patch(
            "research_store.strict_benchmark._get_full_sha",
            return_value="a" * 40,
        ),
        mock.patch(
            "research_store.preflight.probe_postgres",
            return_value="PostgreSQL OK",
        ),
        mock.patch(
            "research_store.preflight.probe_valkey",
            return_value="Valkey OK",
        ),
        mock.patch(
            "research_store.preflight.probe_firecrawl",
            return_value="Firecrawl OK",
        ),
        mock.patch(
            "research_store.preflight.probe_embedding",
            return_value=("Embedding OK", [1.0]),
        ),
        mock.patch(
            "research_store.preflight.probe_qdrant",
            return_value="Qdrant OK",
        ),
        mock.patch(
            "research_store.preflight.probe_reranker",
            return_value="Reranker OK",
        ),
        mock.patch(
            "research_store.preflight.probe_generative",
            return_value="Generative OK",
        ),
        mock.patch(
            "research_store.preflight.probe_resources",
            return_value="Resources OK",
        ),
        mock.patch(
            "research_store.preflight.probe_index_worker",
            return_value="Index worker OK",
        ),
    )


def _run_preflight_with_patches(
    tmp_path: Path,
    *,
    current_sha: str = "a" * 40,
    valkey_error: Exception | None = None,
):
    from contextlib import ExitStack

    dataset = tmp_path / "benchmark.json"
    dataset.write_text("{}", encoding="utf-8")
    patches = list(_successful_probe_patches())
    patches[0] = mock.patch(
        "research_store.strict_benchmark._get_full_sha",
        return_value=current_sha,
    )
    if valkey_error is not None:
        patches[2] = mock.patch(
            "research_store.preflight.probe_valkey",
            side_effect=valkey_error,
        )

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        return _preflight_check(
            database_url="postgresql://test",
            blob_root=tmp_path / "blobs",
            qdrant_url="http://qdrant",
            qdrant_api_key="",
            dataset_path=dataset,
            campaign_dir=tmp_path / "campaign",
            candidate_sha="a" * 40,
        )


def test_valkey_unavailable_fails_complete_preflight(tmp_path):
    ok, errors = _run_preflight_with_patches(
        tmp_path,
        valkey_error=RuntimeError("Valkey unavailable"),
    )

    assert ok is False
    assert any("Valkey queue round-trip failed" in error for error in errors)


def test_candidate_sha_mismatch_fails_complete_preflight(tmp_path):
    ok, errors = _run_preflight_with_patches(
        tmp_path,
        current_sha="b" * 40,
    )

    assert ok is False
    assert any("does not match candidate SHA" in error for error in errors)


def test_local_structured_gateway_disables_thinking_by_default(monkeypatch):
    import model_gateway

    captured = {}

    monkeypatch.setattr(
        model_gateway,
        "probe_local",
        lambda *_args, **_kwargs: {"status": "available", "models": ["chat"]},
    )

    def request_json(url, payload, headers, timeout):
        captured.update(
            {"url": url, "payload": payload, "headers": headers, "timeout": timeout}
        )
        return (
            {
                "id": "request-1",
                "model": "chat",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"status":"ok"}',
                            "reasoning_content": "hidden reasoning",
                        },
                    }
                ],
                "usage": {"completion_tokens": 4},
            },
            "request-1",
            200,
        )

    monkeypatch.setattr(model_gateway, "_request_json", request_json)
    result = model_gateway.call_structured(
        provider="local",
        model="chat",
        system_prompt="Return JSON.",
        user_prompt='{"status":"ok"}',
        schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"status": {"const": "ok"}},
            "required": ["status"],
        },
        max_attempts=1,
    )

    assert result.error == ""
    assert result.value == {"status": "ok"}
    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert result.attempts[0]["thinking_enabled"] is False
    assert result.attempts[0]["reasoning_excerpt"] == "hidden reasoning"
    assert result.provenance["thinking_enabled"] is False


def test_campaign_run_preserves_completed_orchestration_outcome():
    run = CampaignRun(orchestration_outcome="completed")

    assert run.orchestration_outcome == "completed"


def test_reproducibility_accepts_matching_not_applicable_token_metrics():
    def result(campaign_id: str) -> ReleaseBenchmarkResult:
        run_id = uuid4()
        performance = _performance()
        performance = PerformanceMeasurement(
            **{
                **performance.__dict__,
                "total_tokens": 0,
            }
        )
        base_metrics = _performance_metrics(run_id)
        performance_metrics = (
            PerformanceMetric(
                name="total_latency_ms",
                value=performance.total_latency_ms,
                source=MetricSource(
                    table="research_runs",
                    column="completed_at - created_at",
                    run_id=str(run_id),
                    method="duration",
                ),
                formula="regression fixture",
                status=MetricStatus.MEASURED,
            ),
            PerformanceMetric(
                name="semantic_calls",
                value=float(performance.semantic_calls),
                source=MetricSource(
                    table="semantic_calls",
                    column="id",
                    run_id=str(run_id),
                    method="count",
                ),
                formula="regression fixture",
                status=MetricStatus.MEASURED,
            ),
            *(
                PerformanceMetric(
                    name=metric.name,
                    value=0.0 if metric.name == "total_tokens" else metric.value,
                    source=metric.source,
                    formula=metric.formula,
                    status=(
                        MetricStatus.NOT_APPLICABLE
                        if metric.name == "total_tokens"
                        else metric.status
                    ),
                )
                for metric in base_metrics
            ),
        )
        run = CampaignRun(
            campaign_id=campaign_id,
            run_id=str(run_id),
            mode="deterministic_debug",
            objective_id="obj-001",
            quality=_quality(),
            performance=performance,
            quality_metrics=_quality_metrics(run_id),
            performance_metrics=performance_metrics,
            orchestration_outcome="completed",
        )
        return ReleaseBenchmarkResult(
            campaign_id=campaign_id,
            campaign_timestamp="2026-07-31T00:00:00+00:00",
            runs=(run,),
        )

    runner = ReleaseBenchmarkRunner(
        _dataset(),
        ReleaseBenchmarkConfig(
            execution_modes=("autonomous_local", "deterministic_debug"),
            strict=True,
        ),
    )
    comparison = runner.compare_campaigns(result("campaign-a"), result("campaign-b"))

    assert comparison.all_within_tolerance
    assert not any(
        name.endswith(".total_tokens")
        for name, *_values in comparison.performance_tolerances
    )
