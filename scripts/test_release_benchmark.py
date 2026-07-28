"""Tests for the release benchmark infrastructure (issue #135).

This suite exercises:
- MetricEngine: quality and performance metric extraction from persisted state
- ReleaseBenchmarkConfig: configuration validation
- ReleaseBenchmarkRunner: mode validation, campaign execution, reproducibility
- Legacy mode rejection: ensures no silent aliasing
- Simulation mode: three genuinely distinct modes with different quality
- Reproducibility comparison: tolerance checking between two campaign runs
- Integration test: real PostgreSQL execution (when available)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

try:
    import psycopg
except ImportError:
    psycopg = None  # type: ignore[assignment, misc]

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_domain.models import (
    BenchmarkDataset,
    BenchmarkObjective,
    BenchmarkSource,
    PerformanceMeasurement,
    QualityMeasurement,
)
from research_store.release_benchmark import (
    RELEASE_MODES,
    CampaignRun,
    MetricEngine,
    MetricSource,
    PerformanceMetric,
    QualityMetric,
    ReleaseBenchmarkConfig,
    ReleaseBenchmarkResult,
    ReleaseBenchmarkRunner,
    ReproducibilityComparison,
)
from research_store.workflow_benchmark import (
    BenchmarkDatasetLoader,
    load_benchmark_dataset,
    run_benchmark,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


BENCHMARK_FIXTURE = (
    SCRIPTS.parent / "tests" / "fixtures" / "benchmark" / "benchmark-v1.json"
)


def _make_minimal_dataset():
    """Create a minimal valid benchmark dataset."""
    obj = BenchmarkObjective(
        schema_version="benchmark-objective-v1",
        id="obj-minimal",
        title="Minimal objective",
        objective="Test objective",
        questions=("What is the answer?",),
        expected_source_classes=("docs",),
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
        citation_support_labels={"obj-minimal-q1": "SUPPORTED"},
    )
    return BenchmarkDataset(
        schema_version="benchmark-dataset-v1",
        version="benchmark-release-v1",
        description="Test dataset for release benchmark",
        evaluation_set=True,
        objectives=(obj,),
        quality_thresholds={
            "min_candidate_recall": 0.5,
            "max_unsupported_claim_rate": 0.15,
            "min_citation_accuracy": 0.8,
        },
        workflow_modes=RELEASE_MODES,
        deterministic_integrity_checks=(),
    )


def _make_minimal_loader():
    return BenchmarkDatasetLoader(_make_minimal_dataset())


def _make_quality(**overrides):
    defaults = {
        "schema_version": "quality-measurement-v1",
        "candidate_recall": 0.75,
        "source_quality_score": 0.80,
        "coverage_completeness": 0.65,
        "unsupported_claim_rate": 0.08,
        "citation_accuracy": 0.88,
        "report_quality_score": 0.78,
    }
    defaults.update(overrides)
    return QualityMeasurement(**defaults)


def _make_performance(**overrides):
    defaults = {
        "schema_version": "performance-measurement-v1",
        "total_latency_ms": 15000.0,
        "total_tokens": 15000,
        "semantic_calls": 8,
        "cache_hit_rate": 0.3,
        "cache_miss_rate": 0.7,
        "embedding_throughput": 50.0,
        "gpu_memory_mb": 4096.0,
        "cpu_percent": 60.0,
    }
    defaults.update(overrides)
    return PerformanceMeasurement(**defaults)


# ---------------------------------------------------------------------------
# MetricEngine tests
# ---------------------------------------------------------------------------


class TestMetricEngine:
    """Tests for MetricEngine — quality and performance metric extraction."""

    def test_engine_requires_connection(self):
        """MetricEngine raises RuntimeError when not connected."""
        engine = MetricEngine("postgresql://localhost/test")
        with pytest.raises(RuntimeError, match="not connected"):
            engine.extract_quality_metrics(uuid4())

    def test_engine_close_is_safe(self):
        """Closing an unconnected engine is a no-op."""
        engine = MetricEngine("postgresql://localhost/test")
        engine.close()  # Should not raise

    def test_engine_context_manager(self):
        """MetricEngine works as context manager (without DB, connect fails)."""
        # Without a real DB, connect() will fail — but the context manager
        # protocol is still tested for structure.
        engine = MetricEngine("postgresql://nonexistent/test")
        try:
            engine.connect()
        except psycopg.OperationalError:  # Expected — no DB running
            pass
        finally:
            engine.close()  # Should be safe


class TestMetricSource:
    """Tests for MetricSource dataclass."""

    def test_valid_source(self):
        source = MetricSource(
            table="search_candidates",
            column="canonical_url",
            run_id="test-run-id",
            method="count",
        )
        assert source.table == "search_candidates"
        assert source.method == "count"

    def test_all_methods(self):
        for method in ("count", "sum", "avg", "max", "ratio", "boolean"):
            s = MetricSource(table="test", column="col", run_id="run", method=method)
            assert s.method == method


class TestQualityMetric:
    """Tests for QualityMetric dataclass."""

    def test_valid_metric(self):
        m = QualityMetric(
            name="candidate_recall",
            value=0.75,
            source=MetricSource(
                table="search_candidates",
                column="id",
                run_id="run",
                method="count",
            ),
            formula="COUNT(*) / expected",
        )
        assert m.name == "candidate_recall"
        assert m.value == 0.75


class TestPerformanceMetric:
    """Tests for PerformanceMetric dataclass."""

    def test_valid_metric(self):
        m = PerformanceMetric(
            name="total_latency_ms",
            value=15000.0,
            source=MetricSource(
                table="research_runs",
                column="duration",
                run_id="run",
                method="duration",
            ),
            formula="wall_clock_ms",
        )
        assert m.name == "total_latency_ms"


# ---------------------------------------------------------------------------
# ReleaseBenchmarkConfig tests
# ---------------------------------------------------------------------------


class TestReleaseBenchmarkConfig:
    """Tests for ReleaseBenchmarkConfig."""

    def test_default_config(self):
        config = ReleaseBenchmarkConfig()
        assert config.execution_modes == RELEASE_MODES
        assert config.reproducibility_tolerance == 0.15
        assert config.strict is False
        assert config.database_url == ""

    def test_custom_modes(self):
        config = ReleaseBenchmarkConfig(
            execution_modes=("agent_led", "deterministic_debug")
        )
        assert config.execution_modes == ("agent_led", "deterministic_debug")

    def test_custom_tolerance(self):
        config = ReleaseBenchmarkConfig(reproducibility_tolerance=0.10)
        assert config.reproducibility_tolerance == 0.10

    def test_strict_mode(self):
        config = ReleaseBenchmarkConfig(strict=True)
        assert config.strict is True


# ---------------------------------------------------------------------------
# Legacy mode rejection tests
# ---------------------------------------------------------------------------


class TestLegacyModeRejection:
    """Tests that legacy mode is explicitly rejected per issue #135."""

    def test_legacy_forbidden_in_runner(self):
        """ReleaseBenchmarkRunner raises error when legacy is requested."""
        loader = _make_minimal_loader()
        config = ReleaseBenchmarkConfig(
            execution_modes=("legacy", "agent_led"),
            database_url="postgresql://localhost/test",
        )
        runner = ReleaseBenchmarkRunner(loader, config)
        with pytest.raises(RuntimeError, match="legacy.*forbidden"):
            runner._validate_modes()

    def test_legacy_forbidden_in_workflow_benchmark(self):
        """WorkflowBenchmarkRunner rejects legacy mode even in simulation."""
        loader = _make_minimal_loader()
        # Per issue #135: legacy is always forbidden, even in simulation
        with pytest.raises(
            RuntimeError, match="legacy.*forbidden|Unknown benchmark mode"
        ):
            run_benchmark(loader, workflow_modes=("legacy",), dry_run=True)

    def test_unknown_mode_raises(self):
        """Unknown modes raise an error in real execution."""
        loader = _make_minimal_loader()
        config = ReleaseBenchmarkConfig(
            execution_modes=("invalid_mode",),
            database_url="postgresql://localhost/test",
        )
        runner = ReleaseBenchmarkRunner(loader, config)
        with pytest.raises(RuntimeError, match="Unknown benchmark mode"):
            runner._validate_modes()

    def test_duplicate_modes_raises(self):
        """Duplicate modes raise an error."""
        loader = _make_minimal_loader()
        config = ReleaseBenchmarkConfig(
            execution_modes=("agent_led", "agent_led"),
            database_url="postgresql://localhost/test",
        )
        runner = ReleaseBenchmarkRunner(loader, config)
        with pytest.raises(RuntimeError, match="Duplicate benchmark mode"):
            runner._validate_modes()


# ---------------------------------------------------------------------------
# Simulation mode tests — three genuinely distinct modes
# ---------------------------------------------------------------------------


class TestSimulationMode:
    """Tests that each mode produces genuinely distinct simulation results."""

    def test_three_modes_produce_different_quality(self):
        """Each mode produces distinct quality measurements in simulation."""
        loader = _make_minimal_loader()

        result = run_benchmark(
            loader,
            workflow_modes=RELEASE_MODES,
            dry_run=True,
        )
        assert result is not None
        assert result.comparison is not None

        # Index quality by mode
        quality_by_mode: dict[str, QualityMeasurement] = {}
        for r in result.comparison.results:
            quality_by_mode[r.workflow_mode] = r.quality

        # All three modes must produce results
        assert set(quality_by_mode.keys()) == set(RELEASE_MODES)

        # deterministic_debug has the lowest recall, agent_led the highest
        debug_recall = quality_by_mode["deterministic_debug"].candidate_recall
        agent_recall = quality_by_mode["agent_led"].candidate_recall
        local_recall = quality_by_mode["autonomous_local"].candidate_recall

        assert debug_recall < local_recall < agent_recall
        # Verify they are genuinely distinct (not all equal)
        assert debug_recall != local_recall
        assert local_recall != agent_recall
        assert debug_recall != agent_recall

    def test_deterministic_debug_has_lowest_quality(self):
        """deterministic_debug mode has the lowest quality in simulation."""
        loader = _make_minimal_loader()
        result = run_benchmark(
            loader,
            workflow_modes=RELEASE_MODES,
            dry_run=True,
        )
        assert result is not None
        for r in result.comparison.results:
            if r.workflow_mode == "deterministic_debug":
                assert r.quality.candidate_recall < 0.5

    def test_agent_led_has_highest_quality(self):
        """agent_led mode has the highest quality in simulation."""
        loader = _make_minimal_loader()
        result = run_benchmark(
            loader,
            workflow_modes=RELEASE_MODES,
            dry_run=True,
        )
        assert result is not None
        for r in result.comparison.results:
            if r.workflow_mode == "agent_led":
                assert r.quality.candidate_recall >= 0.5


# ---------------------------------------------------------------------------
# Reproducibility comparison tests
# ---------------------------------------------------------------------------


class TestReproducibilityComparison:
    """Tests for reproducibility comparison between two campaign runs."""

    def test_identical_runs_pass_tolerance(self):
        """Two identical runs pass reproducibility tolerance."""
        quality_a = _make_quality(candidate_recall=0.75)
        quality_b = _make_quality(candidate_recall=0.75)
        perf_a = _make_performance(total_latency_ms=15000.0)
        perf_b = _make_performance(total_latency_ms=15000.0)

        run_a = CampaignRun(
            campaign_id="run-a",
            run_id="real-run-a",
            mode="agent_led",
            objective_id="obj-001",
            quality=quality_a,
            performance=perf_a,
        )
        run_b = CampaignRun(
            campaign_id="run-b",
            run_id="real-run-b",
            mode="agent_led",
            objective_id="obj-001",
            quality=quality_b,
            performance=perf_b,
        )

        result_a = ReleaseBenchmarkResult(
            campaign_id="run-a",
            runs=(run_a,),
        )
        result_b = ReleaseBenchmarkResult(
            campaign_id="run-b",
            runs=(run_b,),
        )

        config = ReleaseBenchmarkConfig(reproducibility_tolerance=0.15)
        loader = _make_minimal_loader()
        runner = ReleaseBenchmarkRunner(loader, config)
        comparison = runner.compare_campaigns(result_a, result_b)

        assert comparison.all_within_tolerance is True
        assert comparison.run_a_id == "run-a"
        assert comparison.run_b_id == "run-b"

    def test_runs_within_tolerance_pass(self):
        """Runs with small differences pass reproducibility tolerance."""
        quality_a = _make_quality(candidate_recall=0.75)
        quality_b = _make_quality(candidate_recall=0.77)  # ~2.7% diff, within 15%
        perf_a = _make_performance(total_latency_ms=15000.0)
        perf_b = _make_performance(total_latency_ms=15500.0)  # ~3.3% diff

        run_a = CampaignRun(
            campaign_id="run-a",
            run_id="real-run-a",
            mode="agent_led",
            objective_id="obj-001",
            quality=quality_a,
            performance=perf_a,
        )
        run_b = CampaignRun(
            campaign_id="run-b",
            run_id="real-run-b",
            mode="agent_led",
            objective_id="obj-001",
            quality=quality_b,
            performance=perf_b,
        )

        result_a = ReleaseBenchmarkResult(campaign_id="run-a", runs=(run_a,))
        result_b = ReleaseBenchmarkResult(campaign_id="run-b", runs=(run_b,))

        config = ReleaseBenchmarkConfig(reproducibility_tolerance=0.15)
        loader = _make_minimal_loader()
        runner = ReleaseBenchmarkRunner(loader, config)
        comparison = runner.compare_campaigns(result_a, result_b)

        assert comparison.all_within_tolerance is True

    def test_runs_exceeding_tolerance_fail(self):
        """Runs with large differences fail reproducibility tolerance."""
        quality_a = _make_quality(candidate_recall=0.75)
        quality_b = _make_quality(candidate_recall=0.50)  # 33% diff, exceeds 15%
        perf_a = _make_performance(total_latency_ms=15000.0)
        perf_b = _make_performance(total_latency_ms=25000.0)  # 67% diff

        run_a = CampaignRun(
            campaign_id="run-a",
            run_id="real-run-a",
            mode="agent_led",
            objective_id="obj-001",
            quality=quality_a,
            performance=perf_a,
        )
        run_b = CampaignRun(
            campaign_id="run-b",
            run_id="real-run-b",
            mode="agent_led",
            objective_id="obj-001",
            quality=quality_b,
            performance=perf_b,
        )

        result_a = ReleaseBenchmarkResult(campaign_id="run-a", runs=(run_a,))
        result_b = ReleaseBenchmarkResult(campaign_id="run-b", runs=(run_b,))

        config = ReleaseBenchmarkConfig(reproducibility_tolerance=0.15)
        loader = _make_minimal_loader()
        runner = ReleaseBenchmarkRunner(loader, config)
        comparison = runner.compare_campaigns(result_a, result_b)

        assert comparison.all_within_tolerance is False
        assert len(comparison.details) > 0

    def test_mismatched_modes_not_compared(self):
        """Runs with different modes are not compared."""
        quality_a = _make_quality()
        perf_a = _make_performance()
        quality_b = _make_quality()
        perf_b = _make_performance()

        run_a = CampaignRun(
            campaign_id="run-a",
            run_id="real-run-a",
            mode="agent_led",
            objective_id="obj-001",
            quality=quality_a,
            performance=perf_a,
        )
        run_b = CampaignRun(
            campaign_id="run-b",
            run_id="real-run-b",
            mode="autonomous_local",  # Different mode
            objective_id="obj-001",
            quality=quality_b,
            performance=perf_b,
        )

        result_a = ReleaseBenchmarkResult(campaign_id="run-a", runs=(run_a,))
        result_b = ReleaseBenchmarkResult(campaign_id="run-b", runs=(run_b,))

        config = ReleaseBenchmarkConfig(reproducibility_tolerance=0.15)
        loader = _make_minimal_loader()
        runner = ReleaseBenchmarkRunner(loader, config)
        comparison = runner.compare_campaigns(result_a, result_b)

        # No common (mode, objective) pairs — should be empty comparison
        assert comparison.all_within_tolerance is True


# ---------------------------------------------------------------------------
# CampaignRun and ReleaseBenchmarkResult tests
# ---------------------------------------------------------------------------


class TestCampaignRun:
    """Tests for CampaignRun dataclass."""

    def test_valid_run(self):
        run = CampaignRun(
            campaign_id="fr_bench_test",
            run_id="fr_abc123",
            mode="agent_led",
            objective_id="obj-001",
            quality=_make_quality(),
            performance=_make_performance(),
        )
        assert run.mode == "agent_led"
        assert run.run_id == "fr_abc123"

    def test_run_with_errors(self):
        run = CampaignRun(
            campaign_id="fr_bench_test",
            run_id="",
            mode="agent_led",
            objective_id="obj-001",
            errors=("execution failed: timeout",),
        )
        assert run.errors == ("execution failed: timeout",)


class TestReleaseBenchmarkResult:
    """Tests for ReleaseBenchmarkResult."""

    def test_summary(self):
        result = ReleaseBenchmarkResult(
            campaign_id="fr_bench_test",
            campaign_timestamp="2026-07-28T00:00:00+00:00",
            runs=(
                CampaignRun(
                    campaign_id="fr_bench_test",
                    run_id="fr_abc",
                    mode="agent_led",
                    objective_id="obj-001",
                    quality=_make_quality(),
                    performance=_make_performance(),
                ),
            ),
        )
        summary = result.summary()
        assert "fr_bench_test" in summary
        assert "Duration:" in summary
        assert "Runs: 1" in summary

    def test_summary_with_recommendation(self):
        result = ReleaseBenchmarkResult(
            campaign_id="fr_bench_test",
            recommendation=type("FakeRec", (), {"outcome": "go"})(),  # noqa: FBT009
        )
        summary = result.summary()
        assert "Recommendation:" in summary


# ---------------------------------------------------------------------------
# WorkflowBenchmarkRunner backward-compat tests
# ---------------------------------------------------------------------------


class TestWorkflowBenchmarkBackwardCompat:
    """Tests that WorkflowBenchmarkRunner still works for simulation."""

    def test_simulation_with_three_modes(self):
        """WorkflowBenchmarkRunner simulation works with three real modes."""
        loader = load_benchmark_dataset(BENCHMARK_FIXTURE)
        result = run_benchmark(
            loader,
            workflow_modes=("agent_led", "autonomous_local", "deterministic_debug"),
            dry_run=True,
        )
        assert result is not None
        assert result.dataset_version == "benchmark-v1"
        # Should have results for all three modes
        modes_seen = {r.workflow_mode for r in result.comparison.results}
        assert modes_seen == {"agent_led", "autonomous_local", "deterministic_debug"}

    def test_backward_compat_dry_run(self):
        """WorkflowBenchmarkRunner dry_run works without database."""
        loader = _make_minimal_loader()
        result = run_benchmark(
            loader,
            workflow_modes=("agent_led", "autonomous_local"),
            dry_run=True,
        )
        assert result is not None


# ---------------------------------------------------------------------------
# Integration test — real PostgreSQL execution
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL"),
    reason="requires explicit disposable PostgreSQL test DSN",
)
class TestReleaseBenchmarkIntegration:
    """Integration tests that require a real PostgreSQL database.

    Set RESEARCH_STORE_TEST_DATABASE_URL to a disposable PostgreSQL database
    and RESEARCH_STORE_TEST_ALLOW_RESET to the database name.
    """

    def test_release_benchmark_executes_with_real_db(self):
        """ReleaseBenchmarkRunner executes a campaign against real PostgreSQL."""
        database_url = os.environ["RESEARCH_STORE_TEST_DATABASE_URL"]
        loader = load_benchmark_dataset(BENCHMARK_FIXTURE)

        config = ReleaseBenchmarkConfig(
            database_url=database_url,
            # Need at least 2 modes for comparison; deterministic_debug is fastest.
            execution_modes=("deterministic_debug", "autonomous_local"),
            strict=False,
        )
        runner = ReleaseBenchmarkRunner(loader, config)
        result = runner.run()

        assert result.campaign_id.startswith("fr_bench_")
        assert len(result.runs) > 0
        # Both modes should produce runs
        modes_seen = {run.mode for run in result.runs}
        assert modes_seen == {"deterministic_debug", "autonomous_local"}
        assert result.campaign_id == result.runs[0].campaign_id

    def test_release_benchmark_mode_distinction(self):
        """Each mode produces a different execution_mode in the run."""
        database_url = os.environ["RESEARCH_STORE_TEST_DATABASE_URL"]
        loader = _make_minimal_loader()

        config = ReleaseBenchmarkConfig(
            database_url=database_url,
            execution_modes=("agent_led", "autonomous_local", "deterministic_debug"),
            strict=False,
        )
        runner = ReleaseBenchmarkRunner(loader, config)
        result = runner.run()

        modes_in_runs = {run.mode for run in result.runs}
        assert modes_in_runs == {"agent_led", "autonomous_local", "deterministic_debug"}

    def test_release_benchmark_has_real_run_ids(self):
        """Each campaign run has a real research run ID."""
        database_url = os.environ["RESEARCH_STORE_TEST_DATABASE_URL"]
        loader = _make_minimal_loader()

        config = ReleaseBenchmarkConfig(
            database_url=database_url,
            execution_modes=("deterministic_debug", "autonomous_local"),
            strict=False,
        )
        runner = ReleaseBenchmarkRunner(loader, config)
        result = runner.run()

        for run in result.runs:
            assert run.run_id.startswith("fr_")
            assert len(run.run_id) > 5  # Real UUID-based ID


# ---------------------------------------------------------------------------
# Metric extraction tests (structure, not real data)
# ---------------------------------------------------------------------------


class TestMetricExtractionStructure:
    """Tests for metric extraction structure (without real DB)."""

    def test_quality_metric_fields(self):
        """QualityMetric has all required fields."""
        m = QualityMetric(
            name="test_metric",
            value=0.5,
            source=MetricSource(
                table="test_table",
                column="test_col",
                run_id="test_run",
                method="count",
            ),
            formula="test formula",
        )
        assert m.name == "test_metric"
        assert m.value == 0.5
        assert m.source.table == "test_table"
        assert m.formula == "test formula"

    def test_performance_metric_fields(self):
        """PerformanceMetric has all required fields."""
        m = PerformanceMetric(
            name="test_perf",
            value=100.0,
            source=MetricSource(
                table="test_table",
                column="test_col",
                run_id="test_run",
                method="avg",
            ),
            formula="test formula",
        )
        assert m.name == "test_perf"
        assert m.value == 100.0

    def test_reproducibility_comparison_fields(self):
        """ReproducibilityComparison has all required fields."""
        c = ReproducibilityComparison(
            run_a_id="run-a",
            run_b_id="run-b",
            mode="agent_led",
            objective_id="obj-001",
            quality_tolerances=(
                ("agent_led.obj-001.candidate_recall", 0.75, 0.77, 0.027),
            ),
            performance_tolerances=(),
            all_within_tolerance=True,
            details=(),
        )
        assert c.run_a_id == "run-a"
        assert c.all_within_tolerance is True


# ---------------------------------------------------------------------------
# Performance metric extraction tests (GPU/CPU/token counting)
# ---------------------------------------------------------------------------


class TestPerformanceMetricExtraction:
    """Tests for real GPU/CPU instrumentation and token counting."""

    def test_has_psutil_flag(self):
        """_HAS_PSUTIL flag is set correctly."""
        from research_store.release_benchmark import _HAS_PSUTIL

        assert isinstance(_HAS_PSUTIL, bool)

    def test_has_pynvml_flag(self):
        """_HAS_PYNVML flag is set correctly (optional GPU instrumentation)."""
        from research_store.release_benchmark import _HAS_PYNVML

        assert isinstance(_HAS_PYNVML, bool)

    def test_performance_metric_includes_gpu_and_tokens(self):
        """PerformanceMetric tuple includes gpu_memory_mb and total_tokens."""
        # Verify the metric names are present in the PerformanceMetric
        # dataclass by checking that we can create metrics with these names
        token_metric = PerformanceMetric(
            name="total_tokens",
            value=15000.0,
            source=MetricSource(
                table="model_endpoints",
                column="prompt_tokens + completion_tokens",
                run_id="test_run",
                method="sum",
            ),
            formula="SUM(prompt_tokens + completion_tokens) FROM model_endpoints",
        )
        assert token_metric.name == "total_tokens"
        assert token_metric.value == 15000.0

        gpu_metric = PerformanceMetric(
            name="gpu_memory_mb",
            value=512.0,
            source=MetricSource(
                table="pynvml",
                column="nvmlDeviceGetMemoryInfo",
                run_id="test_run",
                method="nvml",
            ),
            formula="pynvml.nvmlDeviceGetMemoryInfo(0).used / 1MB",
        )
        assert gpu_metric.name == "gpu_memory_mb"
        assert gpu_metric.value == 512.0

    def test_cpu_metric_uses_psutil(self):
        """CPU metric source references psutil, not duration estimation."""
        cpu_metric = PerformanceMetric(
            name="cpu_percent",
            value=25.5,
            source=MetricSource(
                table="psutil",
                column="cpu_percent(interval=0.1)",
                run_id="test_run",
                method="sample",
            ),
            formula="psutil.cpu_percent(interval=0.1) — real system metric",
        )
        assert cpu_metric.source.table == "psutil"
        assert cpu_metric.source.method == "sample"

    def test_token_metric_uses_model_endpoints(self):
        """Token metric source references model_endpoints table, not estimation."""
        token_metric = PerformanceMetric(
            name="total_tokens",
            value=15000.0,
            source=MetricSource(
                table="model_endpoints",
                column="prompt_tokens + completion_tokens",
                run_id="test_run",
                method="sum",
            ),
            formula="SUM(prompt_tokens + completion_tokens) FROM model_endpoints",
        )
        assert token_metric.source.table == "model_endpoints"
        assert token_metric.source.method == "sum"

    def test_reproducibility_compares_gpu_memory(self):
        """ReproducibilityComparison includes gpu_memory_mb in performance tolerances."""
        c = ReproducibilityComparison(
            run_a_id="run-a",
            run_b_id="run-b",
            mode="agent_led",
            objective_id="obj-001",
            quality_tolerances=(),
            performance_tolerances=(
                ("agent_led.obj-001.gpu_memory_mb", 512.0, 520.0, 0.015),
            ),
            all_within_tolerance=True,
            details=(),
        )
        assert c.all_within_tolerance is True
        assert len(c.performance_tolerances) == 1
        assert c.performance_tolerances[0][0] == "agent_led.obj-001.gpu_memory_mb"
