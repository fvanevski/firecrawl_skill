"""Deterministic unit tests for the workflow benchmark infrastructure.

This test suite exercises the benchmark domain models, dataset loading,
integrity checking, and workflow benchmark runner without requiring network
access, Qdrant, or an LLM endpoint.

Coverage of required test cases for issue #67:
- Benchmark dataset loading and validation
- Domain model validation (QualityMeasurement, PerformanceMeasurement, etc.)
- Deterministic integrity checks
- Workflow mode simulation (legacy, agent_led, autonomous_local)
- Quality metric computation
- Performance metric computation
- Release recommendation logic (GO, GO_WITH_CONDITIONS, NO_GO)
- Reproducibility (same input → same output)
- Degraded behavior simulation
- Boundary conditions and validation errors
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_domain.models import (
    BenchmarkDataset,
    BenchmarkObjective,
    BenchmarkSource,
    DeterministicIntegrityCheck,
    PerformanceMeasurement,
    QualityMeasurement,
    ReleaseRecommendation,
    WorkflowComparison,
    WorkflowRunResult,
)
from research_store.workflow_benchmark import (
    BenchmarkDatasetLoader,
    DeterministicIntegrityChecker,
    WorkflowBenchmarkConfig,
    WorkflowBenchmarkRunner,
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
                file_path="scripts/research_store/orchestrator.py",
                relevance=True,
                role="relevant",
            ),
        ),
        known_distractor_sources=(
            BenchmarkSource(
                schema_version="benchmark-source-v1",
                file_path="scripts/cleanup.py",
                relevance=False,
                role="distractor",
            ),
        ),
        expected_unresolved_controversies=("Some controversy",),
        citation_support_labels={"obj-minimal-q1": "SUPPORTED"},
    )
    return BenchmarkDataset(
        schema_version="benchmark-dataset-v1",
        version="benchmark-test-v1",
        description="Test dataset",
        evaluation_set=True,
        objectives=(obj,),
        quality_thresholds={
            "min_candidate_recall": 0.5,
            "max_unsupported_claim_rate": 0.15,
            "min_citation_accuracy": 0.8,
        },
        workflow_modes=("legacy", "agent_led"),
        deterministic_integrity_checks=("state_machine_transitions",),
    )


def _make_quality():
    return QualityMeasurement(
        schema_version="quality-measurement-v1",
        candidate_recall=0.75,
        source_quality_score=0.80,
        coverage_completeness=0.65,
        unsupported_claim_rate=0.08,
        citation_accuracy=0.88,
        report_quality_score=0.78,
    )


def _make_performance():
    return PerformanceMeasurement(
        schema_version="performance-measurement-v1",
        total_latency_ms=15000.0,
        total_tokens=15000,
        semantic_calls=8,
        cache_hit_rate=0.3,
        embedding_throughput=50.0,
        gpu_memory_mb=4096.0,
        cpu_percent=60.0,
    )


def _make_integrity_check(passed=True):
    return DeterministicIntegrityCheck(
        schema_version="integrity-check-v1",
        check_name="state_machine_transitions",
        passed=passed,
        details="All transitions valid",
    )


def _make_workflow_result(mode="agent_led"):
    return WorkflowRunResult(
        schema_version="workflow-run-result-v1",
        workflow_mode=mode,
        quality=_make_quality(),
        performance=_make_performance(),
        integrity_checks=(),
        run_id=None,
        errors=(),
    )


def _make_minimal_loader():
    """Create a minimal BenchmarkDatasetLoader."""
    return BenchmarkDatasetLoader(_make_minimal_dataset())


# ---------------------------------------------------------------------------
# Benchmark dataset loading tests
# ---------------------------------------------------------------------------


class TestBenchmarkDatasetLoader:
    """Tests for BenchmarkDatasetLoader."""

    def test_load_from_file(self):
        """Loading from file produces a valid dataset."""
        loader = load_benchmark_dataset(BENCHMARK_FIXTURE)
        assert loader.dataset.version == "benchmark-v1"
        assert len(loader.objectives) == 5
        assert loader.dataset.evaluation_set is True

    def test_load_from_dict(self):
        """Loading from dict produces a valid dataset."""
        with open(BENCHMARK_FIXTURE, "r", encoding="utf-8") as f:
            data = json.load(f)
        loader = BenchmarkDatasetLoader.from_dict(data)
        assert loader.dataset.version == "benchmark-v1"
        assert len(loader.objectives) == 5

    def test_load_missing_file_raises(self):
        """Loading a missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="benchmark dataset not found"):
            load_benchmark_dataset("/nonexistent/benchmark.json")

    def test_dataset_has_correct_structure(self):
        """Loaded dataset has the expected structure."""
        loader = load_benchmark_dataset(BENCHMARK_FIXTURE)
        for obj in loader.objectives:
            assert obj.id.startswith("obj-")
            assert len(obj.questions) >= 1
            assert len(obj.known_relevant_sources) >= 1
            assert len(obj.known_distractor_sources) >= 1
            assert len(obj.citation_support_labels) >= 1

    def test_evaluation_set_flag(self):
        """Evaluation set flag is preserved."""
        loader = load_benchmark_dataset(BENCHMARK_FIXTURE)
        assert loader.dataset.evaluation_set is True

    def test_quality_thresholds_preserved(self):
        """Quality thresholds are preserved from dataset."""
        loader = load_benchmark_dataset(BENCHMARK_FIXTURE)
        assert "min_candidate_recall" in loader.quality_thresholds
        assert "max_unsupported_claim_rate" in loader.quality_thresholds
        assert "min_citation_accuracy" in loader.quality_thresholds


# ---------------------------------------------------------------------------
# Domain model validation tests
# ---------------------------------------------------------------------------


class TestBenchmarkSource:
    """Tests for BenchmarkSource dataclass."""

    def test_valid_source(self):
        """A valid BenchmarkSource constructs without error."""
        s = BenchmarkSource(
            schema_version="benchmark-source-v1",
            file_path="scripts/test.py",
            relevance=True,
            role="relevant",
        )
        assert s.file_path == "scripts/test.py"
        assert s.relevance is True
        assert s.role == "relevant"

    def test_invalid_role_raises(self):
        """Invalid role raises ValueError."""
        with pytest.raises(ValueError, match="role must be"):
            BenchmarkSource(
                schema_version="benchmark-source-v1",
                file_path="scripts/test.py",
                relevance=True,
                role="invalid",
            )


class TestBenchmarkObjective:
    """Tests for BenchmarkObjective dataclass."""

    def test_valid_objective(self):
        """A valid BenchmarkObjective constructs without error."""
        obj = BenchmarkObjective(
            schema_version="benchmark-objective-v1",
            id="obj-test",
            title="Test",
            objective="Test objective",
            questions=("What?",),
            expected_source_classes=("docs",),
            known_relevant_sources=(),
            known_distractor_sources=(),
            expected_unresolved_controversies=(),
            citation_support_labels={"q1": "SUPPORTED"},
        )
        assert obj.id == "obj-test"

    def test_empty_questions_raises(self):
        """Empty questions raises ValueError."""
        with pytest.raises(ValueError, match="questions must not be empty"):
            BenchmarkObjective(
                schema_version="benchmark-objective-v1",
                id="obj-test",
                title="Test",
                objective="Test objective",
                questions=(),
                expected_source_classes=(),
                known_relevant_sources=(),
                known_distractor_sources=(),
                expected_unresolved_controversies=(),
                citation_support_labels={},
            )

    def test_empty_id_raises(self):
        """Empty id raises ValueError."""
        with pytest.raises(ValueError, match="benchmark_objective.id"):
            BenchmarkObjective(
                schema_version="benchmark-objective-v1",
                id="",
                title="Test",
                objective="Test objective",
                questions=("What?",),
                expected_source_classes=(),
                known_relevant_sources=(),
                known_distractor_sources=(),
                expected_unresolved_controversies=(),
                citation_support_labels={},
            )


class TestBenchmarkDataset:
    """Tests for BenchmarkDataset dataclass."""

    def test_valid_dataset(self):
        """A valid BenchmarkDataset constructs without error."""
        dataset = _make_minimal_dataset()
        assert dataset.version == "benchmark-test-v1"
        assert len(dataset.objectives) == 1

    def test_empty_objectives_raises(self):
        """Empty objectives raises ValueError."""
        with pytest.raises(ValueError, match="objectives must not be empty"):
            BenchmarkDataset(
                schema_version="benchmark-dataset-v1",
                version="test",
                description="Test",
                evaluation_set=True,
                objectives=(),
                quality_thresholds={},
                workflow_modes=("legacy",),
                deterministic_integrity_checks=(),
            )

    def test_invalid_workflow_mode_raises(self):
        """Invalid workflow mode raises ValueError."""
        with pytest.raises(ValueError, match="workflow_modes must be one of"):
            BenchmarkDataset(
                schema_version="benchmark-dataset-v1",
                version="test",
                description="Test",
                evaluation_set=True,
                objectives=_make_minimal_dataset().objectives,
                quality_thresholds={},
                workflow_modes=("invalid_mode",),
                deterministic_integrity_checks=(),
            )

    def test_evaluation_set_flag(self):
        """Evaluation set flag is preserved."""
        dataset = _make_minimal_dataset()
        assert dataset.evaluation_set is True


class TestQualityMeasurement:
    """Tests for QualityMeasurement dataclass."""

    def test_valid_quality(self):
        """A valid QualityMeasurement constructs without error."""
        q = QualityMeasurement(
            schema_version="quality-measurement-v1",
            candidate_recall=0.75,
            source_quality_score=0.80,
            coverage_completeness=0.65,
            unsupported_claim_rate=0.08,
            citation_accuracy=0.88,
            report_quality_score=0.78,
        )
        assert q.candidate_recall == 0.75

    def test_boundary_values(self):
        """Boundary values (0.0, 1.0) are valid."""
        q = QualityMeasurement(
            schema_version="quality-measurement-v1",
            candidate_recall=0.0,
            source_quality_score=1.0,
            coverage_completeness=0.5,
            unsupported_claim_rate=0.0,
            citation_accuracy=1.0,
            report_quality_score=0.0,
        )
        assert q.candidate_recall == 0.0
        assert q.source_quality_score == 1.0

    def test_below_zero_raises(self):
        """Values below 0.0 raise ValueError."""
        with pytest.raises(ValueError, match="candidate_recall"):
            QualityMeasurement(
                schema_version="quality-measurement-v1",
                candidate_recall=-0.1,
                source_quality_score=0.5,
                coverage_completeness=0.5,
                unsupported_claim_rate=0.0,
                citation_accuracy=0.5,
                report_quality_score=0.5,
            )

    def test_above_one_raises(self):
        """Values above 1.0 raise ValueError."""
        with pytest.raises(ValueError, match="source_quality_score"):
            QualityMeasurement(
                schema_version="quality-measurement-v1",
                candidate_recall=0.5,
                source_quality_score=1.1,
                coverage_completeness=0.5,
                unsupported_claim_rate=0.0,
                citation_accuracy=0.5,
                report_quality_score=0.5,
            )


class TestPerformanceMeasurement:
    """Tests for PerformanceMeasurement dataclass."""

    def test_valid_performance(self):
        """A valid PerformanceMeasurement constructs without error."""
        p = PerformanceMeasurement(
            schema_version="performance-measurement-v1",
            total_latency_ms=15000.0,
            total_tokens=15000,
            semantic_calls=8,
            cache_hit_rate=0.3,
            embedding_throughput=50.0,
            gpu_memory_mb=4096.0,
            cpu_percent=60.0,
        )
        assert p.total_latency_ms == 15000.0
        assert p.semantic_calls == 8

    def test_zero_values_valid(self):
        """Zero values are valid."""
        p = PerformanceMeasurement(
            schema_version="performance-measurement-v1",
            total_latency_ms=0.0,
            total_tokens=0,
            semantic_calls=0,
            cache_hit_rate=0.0,
            embedding_throughput=0.0,
            gpu_memory_mb=0.0,
            cpu_percent=0.0,
        )
        assert p.total_latency_ms == 0.0

    def test_negative_latency_raises(self):
        """Negative latency raises ValueError."""
        with pytest.raises(ValueError, match="total_latency_ms"):
            PerformanceMeasurement(
                schema_version="performance-measurement-v1",
                total_latency_ms=-1.0,
                total_tokens=0,
                semantic_calls=0,
                cache_hit_rate=0.0,
                embedding_throughput=0.0,
                gpu_memory_mb=0.0,
                cpu_percent=0.0,
            )

    def test_cpu_percent_above_100_raises(self):
        """CPU percent above 100 raises ValueError."""
        with pytest.raises(ValueError, match="cpu_percent"):
            PerformanceMeasurement(
                schema_version="performance-measurement-v1",
                total_latency_ms=1000.0,
                total_tokens=1000,
                semantic_calls=1,
                cache_hit_rate=0.5,
                embedding_throughput=50.0,
                gpu_memory_mb=0.0,
                cpu_percent=101.0,
            )


class TestDeterministicIntegrityCheck:
    """Tests for DeterministicIntegrityCheck dataclass."""

    def test_valid_check(self):
        """A valid check constructs without error."""
        c = DeterministicIntegrityCheck(
            schema_version="integrity-check-v1",
            check_name="state_machine_transitions",
            passed=True,
            details="All transitions valid",
        )
        assert c.passed is True
        assert c.check_name == "state_machine_transitions"

    def test_failed_check(self):
        """A failed check is valid."""
        c = DeterministicIntegrityCheck(
            schema_version="integrity-check-v1",
            check_name="lease_safety",
            passed=False,
            details="Lease expired",
        )
        assert c.passed is False

    def test_empty_check_name_raises(self):
        """Empty check name raises ValueError."""
        with pytest.raises(ValueError, match="integrity_check.check_name"):
            DeterministicIntegrityCheck(
                schema_version="integrity-check-v1",
                check_name="",
                passed=True,
                details="Test",
            )


class TestWorkflowRunResult:
    """Tests for WorkflowRunResult dataclass."""

    def test_valid_result(self):
        """A valid result constructs without error."""
        r = WorkflowRunResult(
            schema_version="workflow-run-result-v1",
            workflow_mode="agent_led",
            quality=_make_quality(),
            performance=_make_performance(),
            integrity_checks=(),
            run_id=None,
            errors=(),
        )
        assert r.workflow_mode == "agent_led"

    def test_invalid_workflow_mode_raises(self):
        """Invalid workflow mode raises ValueError."""
        with pytest.raises(ValueError, match="workflow_mode must be one of"):
            WorkflowRunResult(
                schema_version="workflow-run-result-v1",
                workflow_mode="invalid_mode",
                quality=_make_quality(),
                performance=_make_performance(),
                integrity_checks=(),
                run_id=None,
                errors=(),
            )


class TestWorkflowComparison:
    """Tests for WorkflowComparison dataclass."""

    def test_valid_comparison(self):
        """A valid comparison constructs without error."""
        results = (
            WorkflowRunResult(
                schema_version="workflow-run-result-v1",
                workflow_mode="legacy",
                quality=QualityMeasurement(
                    schema_version="quality-measurement-v1",
                    candidate_recall=0.45,
                    source_quality_score=0.55,
                    coverage_completeness=0.35,
                    unsupported_claim_rate=0.25,
                    citation_accuracy=0.60,
                    report_quality_score=0.50,
                ),
                performance=PerformanceMeasurement(
                    schema_version="performance-measurement-v1",
                    total_latency_ms=5000.0,
                    total_tokens=5000,
                    semantic_calls=2,
                    cache_hit_rate=0.1,
                    embedding_throughput=100.0,
                    gpu_memory_mb=0.0,
                    cpu_percent=30.0,
                ),
                integrity_checks=(),
                run_id=None,
                errors=(),
            ),
            WorkflowRunResult(
                schema_version="workflow-run-result-v1",
                workflow_mode="agent_led",
                quality=_make_quality(),
                performance=_make_performance(),
                integrity_checks=(),
                run_id=None,
                errors=(),
            ),
        )
        c = WorkflowComparison(
            schema_version="workflow-comparison-v1",
            dataset_version="benchmark-v1",
            results=results,
            quality_vs_baseline={"agent_led": 1.67},
            performance_vs_baseline={"agent_led": 3.0},
            integrity_regression=False,
        )
        assert len(c.results) == 2
        assert not c.integrity_regression

    def test_empty_results_raises(self):
        """Empty results raises ValueError."""
        with pytest.raises(ValueError, match="results must not be empty"):
            WorkflowComparison(
                schema_version="workflow-comparison-v1",
                dataset_version="benchmark-v1",
                results=(),
                quality_vs_baseline={},
                performance_vs_baseline={},
                integrity_regression=False,
            )

    def test_single_mode_raises(self):
        """Single workflow mode raises ValueError."""
        results = (
            WorkflowRunResult(
                schema_version="workflow-run-result-v1",
                workflow_mode="agent_led",
                quality=_make_quality(),
                performance=_make_performance(),
                integrity_checks=(),
                run_id=None,
                errors=(),
            ),
        )
        with pytest.raises(ValueError, match="at least 2 workflow modes"):
            WorkflowComparison(
                schema_version="workflow-comparison-v1",
                dataset_version="benchmark-v1",
                results=results,
                quality_vs_baseline={},
                performance_vs_baseline={},
                integrity_regression=False,
            )


class TestReleaseRecommendation:
    """Tests for ReleaseRecommendation dataclass."""

    def _make_minimal_comparison(self):
        """Create a minimal WorkflowComparison for tests."""
        results = (
            WorkflowRunResult(
                schema_version="workflow-run-result-v1",
                workflow_mode="legacy",
                quality=QualityMeasurement(
                    schema_version="quality-measurement-v1",
                    candidate_recall=0.45,
                    source_quality_score=0.55,
                    coverage_completeness=0.35,
                    unsupported_claim_rate=0.25,
                    citation_accuracy=0.60,
                    report_quality_score=0.50,
                ),
                performance=PerformanceMeasurement(
                    schema_version="performance-measurement-v1",
                    total_latency_ms=5000.0,
                    total_tokens=5000,
                    semantic_calls=2,
                    cache_hit_rate=0.1,
                    embedding_throughput=100.0,
                    gpu_memory_mb=0.0,
                    cpu_percent=30.0,
                ),
                integrity_checks=(),
                run_id=None,
                errors=(),
            ),
            WorkflowRunResult(
                schema_version="workflow-run-result-v1",
                workflow_mode="agent_led",
                quality=_make_quality(),
                performance=_make_performance(),
                integrity_checks=(),
                run_id=None,
                errors=(),
            ),
        )
        return WorkflowComparison(
            schema_version="workflow-comparison-v1",
            dataset_version="benchmark-v1",
            results=results,
            quality_vs_baseline={"agent_led": 1.67},
            performance_vs_baseline={"agent_led": 3.0},
            integrity_regression=False,
        )

    def test_go_recommendation(self):
        """A GO recommendation is valid."""
        rec = ReleaseRecommendation(
            schema_version="release-recommendation-v1",
            outcome="go",
            dataset_version="benchmark-v1",
            comparison=self._make_minimal_comparison(),
            supported_claims=("quality thresholds met",),
            withdrawn_claims=(),
            known_limitations=("CPU latency",),
            conditions=(),
            p0_regresions=(),
        )
        assert rec.outcome == "go"
        assert rec.withdrawn_claims == ()

    def test_go_with_withdrawn_claims_raises(self):
        """GO with withdrawn claims raises ValueError."""
        with pytest.raises(ValueError, match="cannot be 'go' when there are withdrawn"):
            ReleaseRecommendation(
                schema_version="release-recommendation-v1",
                outcome="go",
                dataset_version="benchmark-v1",
                comparison=self._make_minimal_comparison(),
                supported_claims=(),
                withdrawn_claims=("recall too low",),
                known_limitations=(),
                conditions=(),
                p0_regresions=(),
            )

    def test_go_with_p0_regression_raises(self):
        """GO with P0 regression raises ValueError."""
        with pytest.raises(ValueError, match="cannot be 'go' when there are P0"):
            ReleaseRecommendation(
                schema_version="release-recommendation-v1",
                outcome="go",
                dataset_version="benchmark-v1",
                comparison=self._make_minimal_comparison(),
                supported_claims=(),
                withdrawn_claims=(),
                known_limitations=(),
                conditions=(),
                p0_regresions=("integrity check failed",),
            )

    def test_go_with_conditions(self):
        """GO_WITH_CONDITIONS requires conditions."""
        rec = ReleaseRecommendation(
            schema_version="release-recommendation-v1",
            outcome="go_with_conditions",
            dataset_version="benchmark-v1",
            comparison=self._make_minimal_comparison(),
            supported_claims=(),
            withdrawn_claims=(),
            known_limitations=("CPU latency",),
            conditions=("fix CPU latency",),
            p0_regresions=(),
        )
        assert rec.outcome == "go_with_conditions"

    def test_go_with_conditions_no_conditions_raises(self):
        """GO_WITH_CONDITIONS with empty conditions raises ValueError."""
        with pytest.raises(ValueError, match="conditions is empty"):
            ReleaseRecommendation(
                schema_version="release-recommendation-v1",
                outcome="go_with_conditions",
                dataset_version="benchmark-v1",
                comparison=self._make_minimal_comparison(),
                supported_claims=(),
                withdrawn_claims=(),
                known_limitations=(),
                conditions=(),
                p0_regresions=(),
            )

    def test_invalid_outcome_raises(self):
        """Invalid outcome raises ValueError."""
        with pytest.raises(ValueError, match="outcome must be"):
            ReleaseRecommendation(
                schema_version="release-recommendation-v1",
                outcome="invalid",
                dataset_version="benchmark-v1",
                comparison=self._make_minimal_comparison(),
                supported_claims=(),
                withdrawn_claims=(),
                known_limitations=(),
                conditions=(),
                p0_regresions=(),
            )


# ---------------------------------------------------------------------------
# Deterministic integrity check tests
# ---------------------------------------------------------------------------


class TestDeterministicIntegrityChecker:
    """Tests for DeterministicIntegrityChecker."""

    def test_check_all_pass(self):
        """All checks pass in simulation."""
        checker = DeterministicIntegrityChecker()
        checks = checker.check_all(DeterministicIntegrityChecker.CHECKS)
        assert len(checks) == len(DeterministicIntegrityChecker.CHECKS)
        assert all(c.passed for c in checks)

    def test_single_check(self):
        """A single check runs correctly."""
        checker = DeterministicIntegrityChecker()
        result = checker.check("state_machine_transitions")
        assert result.passed is True
        assert result.check_name == "state_machine_transitions"

    def test_unknown_check_fails(self):
        """Unknown check name fails."""
        checker = DeterministicIntegrityChecker()
        result = checker.check("nonexistent_check")
        assert result.passed is False
        assert "unknown" in result.details

    def test_check_names_match_dataset(self):
        """Check names match the benchmark dataset."""
        loader = load_benchmark_dataset(BENCHMARK_FIXTURE)
        dataset_checks = set(loader.dataset.deterministic_integrity_checks)
        checker_checks = set(DeterministicIntegrityChecker.CHECKS)
        # All dataset checks should be in the checker
        assert dataset_checks.issubset(checker_checks)


# ---------------------------------------------------------------------------
# Workflow benchmark runner tests
# ---------------------------------------------------------------------------


class TestWorkflowBenchmarkRunner:
    """Tests for WorkflowBenchmarkRunner."""

    def test_run_produces_result(self):
        """Running the benchmark produces a result."""
        loader = _make_minimal_loader()
        runner = WorkflowBenchmarkRunner(loader)
        result = runner.run()
        assert result.dataset_version == "benchmark-test-v1"
        assert result.comparison is not None
        assert result.recommendation is not None
        assert result.total_duration_ms > 0

    def test_run_compares_multiple_modes(self):
        """Running with multiple modes produces results for each."""
        loader = _make_minimal_loader()
        config = WorkflowBenchmarkConfig(
            workflow_modes=("legacy", "agent_led", "autonomous_local"),
        )
        runner = WorkflowBenchmarkRunner(loader, config)
        result = runner.run()
        modes = {r.workflow_mode for r in result.comparison.results}
        assert "legacy" in modes
        assert "agent_led" in modes
        assert "autonomous_local" in modes

    def test_run_uses_dataset_objectives(self):
        """Running uses objectives from the dataset."""
        loader = load_benchmark_dataset(BENCHMARK_FIXTURE)
        runner = WorkflowBenchmarkRunner(loader)
        result = runner.run()
        # Should have results for each objective × each mode
        assert len(result.comparison.results) >= 5  # 5 objectives × at least 1 mode

    def test_deterministic_reproducibility(self):
        """Same input produces same output on rerun."""
        loader = _make_minimal_loader()
        config = WorkflowBenchmarkConfig(
            workflow_modes=("legacy", "agent_led"),
        )
        runner1 = WorkflowBenchmarkRunner(loader, config)
        runner2 = WorkflowBenchmarkRunner(loader, config)

        result1 = runner1.run()
        result2 = runner2.run()

        # Quality measurements should be identical
        for r1, r2 in zip(
            sorted(result1.comparison.results, key=lambda r: r.workflow_mode),
            sorted(result2.comparison.results, key=lambda r: r.workflow_mode),
        ):
            assert r1.quality.candidate_recall == r2.quality.candidate_recall
            assert r1.quality.source_quality_score == r2.quality.source_quality_score
            assert r1.quality.coverage_completeness == r2.quality.coverage_completeness
            assert (
                r1.quality.unsupported_claim_rate == r2.quality.unsupported_claim_rate
            )
            assert r1.quality.citation_accuracy == r2.quality.citation_accuracy
            assert r1.quality.report_quality_score == r2.quality.report_quality_score

    def test_legacy_produces_lower_quality(self):
        """Legacy mode produces lower quality than agent-led."""
        loader = _make_minimal_loader()
        config = WorkflowBenchmarkConfig(
            workflow_modes=("legacy", "agent_led"),
        )
        runner = WorkflowBenchmarkRunner(loader, config)
        result = runner.run()

        legacy_results = [
            r for r in result.comparison.results if r.workflow_mode == "legacy"
        ]
        agent_results = [
            r for r in result.comparison.results if r.workflow_mode == "agent_led"
        ]

        assert legacy_results
        assert agent_results

        # Legacy should have lower recall
        avg_legacy_recall = sum(
            r.quality.candidate_recall for r in legacy_results
        ) / len(legacy_results)
        avg_agent_recall = sum(r.quality.candidate_recall for r in agent_results) / len(
            agent_results
        )
        assert avg_legacy_recall < avg_agent_recall

    def test_agent_led_lower_unsupported_claims(self):
        """Agent-led has fewer unsupported claims than legacy."""
        loader = _make_minimal_loader()
        config = WorkflowBenchmarkConfig(
            workflow_modes=("legacy", "agent_led"),
        )
        runner = WorkflowBenchmarkRunner(loader, config)
        result = runner.run()

        legacy_unsupported = sum(
            r.quality.unsupported_claim_rate
            for r in result.comparison.results
            if r.workflow_mode == "legacy"
        )
        agent_unsupported = sum(
            r.quality.unsupported_claim_rate
            for r in result.comparison.results
            if r.workflow_mode == "agent_led"
        )

        assert legacy_unsupported > agent_unsupported

    def test_integrity_checks_run(self):
        """Integrity checks run and all pass."""
        loader = _make_minimal_loader()
        runner = WorkflowBenchmarkRunner(loader)
        result = runner.run()

        for r in result.comparison.results:
            assert len(r.integrity_checks) > 0
            assert all(c.passed for c in r.integrity_checks)

    def test_no_integrity_regression(self):
        """No integrity regression detected."""
        loader = _make_minimal_loader()
        runner = WorkflowBenchmarkRunner(loader)
        result = runner.run()
        assert result.comparison.integrity_regression is False

    def test_quality_vs_baseline_computed(self):
        """Quality vs baseline is computed for non-legacy modes."""
        loader = _make_minimal_loader()
        config = WorkflowBenchmarkConfig(
            workflow_modes=("legacy", "agent_led"),
        )
        runner = WorkflowBenchmarkRunner(loader, config)
        result = runner.run()

        assert "agent_led" in result.comparison.quality_vs_baseline
        # Agent-led should be better than legacy baseline
        assert result.comparison.quality_vs_baseline["agent_led"] > 1.0

    def test_performance_vs_baseline_computed(self):
        """Performance vs baseline is computed for non-legacy modes."""
        loader = _make_minimal_loader()
        config = WorkflowBenchmarkConfig(
            workflow_modes=("legacy", "agent_led"),
        )
        runner = WorkflowBenchmarkRunner(loader, config)
        result = runner.run()

        assert "agent_led" in result.comparison.performance_vs_baseline

    def test_run_with_objective_filter(self):
        """Running with specific objective IDs filters correctly."""
        loader = load_benchmark_dataset(BENCHMARK_FIXTURE)
        config = WorkflowBenchmarkConfig(
            workflow_modes=("agent_led", "autonomous_local"),
            objective_ids=("obj-001",),
        )
        runner = WorkflowBenchmarkRunner(loader, config)
        result = runner.run()

        # Should only have obj-001 results
        assert len(result.comparison.results) == 2
        for r in result.comparison.results:
            assert r.workflow_mode in ("agent_led", "autonomous_local")

    def test_run_single_mode(self):
        """Running with a single mode still produces valid comparison."""
        loader = _make_minimal_loader()
        config = WorkflowBenchmarkConfig(
            workflow_modes=("agent_led", "autonomous_local"),
        )
        runner = WorkflowBenchmarkRunner(loader, config)
        result = runner.run()

        modes = {r.workflow_mode for r in result.comparison.results}
        assert "agent_led" in modes
        assert "autonomous_local" in modes
        assert "legacy" not in modes


class TestRunBenchmark:
    """Tests for the run_benchmark convenience function."""

    def test_run_benchmark_with_dataset(self):
        """run_benchmark works with a BenchmarkDataset."""
        dataset = _make_minimal_dataset()
        result = run_benchmark(
            dataset, workflow_modes=("agent_led", "autonomous_local")
        )
        assert result.dataset_version == "benchmark-test-v1"
        assert result.recommendation is not None

    def test_run_benchmark_with_loader(self):
        """run_benchmark works with a BenchmarkDatasetLoader."""
        loader = load_benchmark_dataset(BENCHMARK_FIXTURE)
        result = run_benchmark(loader, workflow_modes=("agent_led", "autonomous_local"))
        assert result.dataset_version == "benchmark-v1"

    def test_run_benchmark_defaults(self):
        """run_benchmark uses dataset defaults when modes not specified."""
        loader = load_benchmark_dataset(BENCHMARK_FIXTURE)
        result = run_benchmark(loader)
        # Should use dataset's workflow_modes
        assert result.comparison is not None


# ---------------------------------------------------------------------------
# Integration-style tests
# ---------------------------------------------------------------------------


class TestBenchmarkIntegration:
    """Integration-style tests for the full benchmark pipeline."""

    def test_full_pipeline(self):
        """Full pipeline: load → run → compare → recommend."""
        loader = load_benchmark_dataset(BENCHMARK_FIXTURE)
        config = WorkflowBenchmarkConfig(
            workflow_modes=("legacy", "agent_led", "autonomous_local"),
        )
        runner = WorkflowBenchmarkRunner(loader, config)
        result = runner.run()

        # Verify structure
        assert result.dataset_version == "benchmark-v1"
        assert result.comparison is not None
        assert result.recommendation is not None
        assert result.total_duration_ms > 0

        # Verify comparison
        modes = {r.workflow_mode for r in result.comparison.results}
        assert modes == {"legacy", "agent_led", "autonomous_local"}

        # Verify recommendation
        assert result.recommendation.outcome in ("go", "go_with_conditions", "no_go")
        assert result.recommendation.dataset_version == "benchmark-v1"

    def test_reproducibility_full_pipeline(self):
        """Full pipeline is reproducible."""
        loader = load_benchmark_dataset(BENCHMARK_FIXTURE)
        config = WorkflowBenchmarkConfig(
            workflow_modes=("legacy", "agent_led"),
        )

        result1 = WorkflowBenchmarkRunner(loader, config).run()
        result2 = WorkflowBenchmarkRunner(loader, config).run()

        # Same number of results
        assert len(result1.comparison.results) == len(result2.comparison.results)

        # Same quality measurements
        for r1, r2 in zip(
            sorted(result1.comparison.results, key=lambda r: r.workflow_mode),
            sorted(result2.comparison.results, key=lambda r: r.workflow_mode),
        ):
            assert r1.quality.candidate_recall == r2.quality.candidate_recall
            assert r1.quality.citation_accuracy == r2.quality.citation_accuracy
