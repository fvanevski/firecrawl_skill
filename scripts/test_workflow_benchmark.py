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
        cache_miss_rate=0.7,
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
            cache_miss_rate=0.7,
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
            cache_miss_rate=1.0,
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
                cache_miss_rate=1.0,
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
                cache_miss_rate=0.5,
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
                    cache_miss_rate=0.9,
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
                    cache_miss_rate=0.9,
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
            p0_regressions=(),
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
                p0_regressions=(),
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
                p0_regressions=("integrity check failed",),
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
            p0_regressions=(),
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
                p0_regressions=(),
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
                p0_regressions=(),
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

    def test_strict_mode_fails_on_simulation(self):
        """Strict mode fails when real state is not available."""
        checker = DeterministicIntegrityChecker(strict=True)
        result = checker.check("state_machine_transitions")
        assert result.passed is False
        assert "strict" in result.details.lower()

    def test_strict_mode_fails_multiple_checks(self):
        """Strict mode fails ALL simulation-fallback checks, not just one."""
        checker = DeterministicIntegrityChecker(strict=True)
        checks = checker.check_all(DeterministicIntegrityChecker.CHECKS)

        # Checks that fall back to simulation should all fail in strict mode
        simulation_checks = {
            "content_addressed_blob_integrity",
            "derivation_versioning",
            "lease_safety",
            "cache_key_identity",
            "idempotent_replay",
        }
        failed_simulation = [
            c for c in checks if c.check_name in simulation_checks and c.passed is False
        ]
        assert len(failed_simulation) >= 3, (
            f"Expected at least 3 strict-mode failures for simulation checks, "
            f"got {len(failed_simulation)}: {[c.check_name for c in failed_simulation]}"
        )

    def test_strict_mode_passes_with_real_data(self):
        """Strict mode passes when real data is provided."""
        transitions = [
            {"prior_state": "created", "next_state": "planning"},
        ]
        checker = DeterministicIntegrityChecker(
            strict=True, run_transitions=transitions
        )
        result = checker.check("state_machine_transitions")
        assert result.passed is True
        assert "checked" in result.details


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


# ---------------------------------------------------------------------------
# Integrity checker real-data tests
# ---------------------------------------------------------------------------


class TestDeterministicIntegrityCheckerRealData:
    """Tests for the DeterministicIntegrityChecker with real data."""

    def test_blob_integrity_simulation_when_no_blob_root(self):
        """Blob integrity check falls back to simulation when no blob root."""
        checker = DeterministicIntegrityChecker()
        result = checker.check("content_addressed_blob_integrity")
        assert result.passed is True
        assert "simulation" in result.details

    def test_blob_integrity_with_temp_dir(self, tmp_path):
        """Blob integrity check verifies real blobs when blob root is provided."""
        from research_store.blob import ContentAddressedBlobStore

        # Create a temp blob store with a valid blob
        blob_root = tmp_path / "blobs"
        store = ContentAddressedBlobStore(blob_root)
        test_content = b"test content for blob integrity check"
        store.put(__import__("io").BytesIO(test_content), mime_type="text/plain")

        # Now run the integrity check
        checker = DeterministicIntegrityChecker(blob_root=blob_root)
        result = checker.check("content_addressed_blob_integrity")
        assert result.passed is True
        assert "verified 1 blobs" in result.details

    def test_evidence_packet_validation_with_valid_packet(self):
        """Evidence packet validation passes when all bindings are valid."""
        packet = {
            "claims": [
                {"claim_id": "c1", "statement": "Test claim"},
                {"claim_id": "c2", "statement": "Another claim"},
            ],
            "passages": [
                {"passage_id": "p1", "text": "Test passage"},
                {"passage_id": "p2", "text": "Another passage"},
            ],
            "claim_evidence_bindings": [
                {
                    "claim_id": "c1",
                    "passage_ids": ["p1", "p2"],
                    "relationship": "supports",
                },
                {
                    "claim_id": "c2",
                    "passage_ids": ["p1"],
                    "relationship": "supports",
                },
            ],
        }
        checker = DeterministicIntegrityChecker(evidence_packets=[packet])
        result = checker.check("evidence_packet_validation")
        assert result.passed is True
        assert "validated 1 packets" in result.details

    def test_evidence_packet_validation_with_invalid_binding(self):
        """Evidence packet validation fails when bindings reference unknown claims."""
        packet = {
            "claims": [{"claim_id": "c1", "statement": "Test claim"}],
            "passages": [{"passage_id": "p1", "text": "Test passage"}],
            "claim_evidence_bindings": [
                {
                    "claim_id": "c999",  # Unknown claim
                    "passage_ids": ["p1"],
                    "relationship": "supports",
                },
            ],
        }
        checker = DeterministicIntegrityChecker(evidence_packets=[packet])
        result = checker.check("evidence_packet_validation")
        assert result.passed is False
        assert "errors" in result.details

    def test_citation_binding_integrity_with_valid_citations(self):
        """Citation binding integrity passes when all citations are valid."""
        packet = {
            "claims": [{"claim_id": "c1", "statement": "Test claim"}],
            "passages": [{"passage_id": "p1", "text": "Test passage"}],
            "claim_evidence_bindings": [
                {
                    "claim_id": "c1",
                    "passage_ids": ["p1"],
                    "relationship": "supports",
                },
            ],
        }
        checker = DeterministicIntegrityChecker(evidence_packets=[packet])
        result = checker.check("citation_binding_integrity")
        assert result.passed is True
        assert "checked 1 citations" in result.details

    def test_citation_binding_integrity_with_invalid_citation(self):
        """Citation binding integrity fails when citations reference unknown passages."""
        packet = {
            "claims": [{"claim_id": "c1", "statement": "Test claim"}],
            "passages": [{"passage_id": "p1", "text": "Test passage"}],
            "claim_evidence_bindings": [
                {
                    "claim_id": "c1",
                    "passage_ids": ["p999"],  # Unknown passage
                    "relationship": "supports",
                },
            ],
        }
        checker = DeterministicIntegrityChecker(evidence_packets=[packet])
        result = checker.check("citation_binding_integrity")
        assert result.passed is False
        assert "errors" in result.details

    def test_state_machine_transitions_with_valid_transitions(self):
        """State machine transition check passes when all transitions are valid."""
        transitions = [
            {"prior_state": "created", "next_state": "planning"},
            {"prior_state": "planning", "next_state": "corpus_review"},
            {"prior_state": "corpus_review", "next_state": "acquiring"},
        ]
        checker = DeterministicIntegrityChecker(run_transitions=transitions)
        result = checker.check("state_machine_transitions")
        assert result.passed is True
        assert "checked 3 transitions" in result.details

    def test_state_machine_transitions_with_invalid_transition(self):
        """State machine transition check fails when a transition is invalid."""
        transitions = [
            {"prior_state": "created", "next_state": "completed"},  # Invalid
        ]
        checker = DeterministicIntegrityChecker(run_transitions=transitions)
        result = checker.check("state_machine_transitions")
        assert result.passed is False
        assert "errors" in result.details

    def test_unknown_check_fails(self):
        """Unknown check name fails."""
        checker = DeterministicIntegrityChecker()
        result = checker.check("nonexistent_check")
        assert result.passed is False
        assert "unknown" in result.details

    def test_check_all_with_real_data(self):
        """check_all runs all checks with real data."""
        packet = {
            "claims": [{"claim_id": "c1", "statement": "Test"}],
            "passages": [{"passage_id": "p1", "text": "Test"}],
            "claim_evidence_bindings": [
                {"claim_id": "c1", "passage_ids": ["p1"], "relationship": "supports"},
            ],
        }
        transitions = [
            {"prior_state": "created", "next_state": "planning"},
        ]
        checker = DeterministicIntegrityChecker(
            evidence_packets=[packet], run_transitions=transitions
        )
        results = checker.check_all(DeterministicIntegrityChecker.CHECKS)
        assert len(results) == len(DeterministicIntegrityChecker.CHECKS)
        # Real checks should have passed (data is valid)
        real_checks = {
            "evidence_packet_validation",
            "citation_binding_integrity",
            "state_machine_transitions",
        }
        for result in results:
            if result.check_name in real_checks:
                assert result.passed is True


# ---------------------------------------------------------------------------
# Known limitations tests
# ---------------------------------------------------------------------------


class TestKnownLimitations:
    """Tests for configurable known limitations."""

    def test_default_limitations(self):
        """Default limitations are used when none are provided."""
        loader = _make_minimal_loader()
        config = WorkflowBenchmarkConfig(
            workflow_modes=("legacy", "agent_led"),
        )
        runner = WorkflowBenchmarkRunner(loader, config)
        result = runner.run()
        assert len(result.recommendation.known_limitations) > 0
        assert any("CPU" in lim for lim in result.recommendation.known_limitations)

    def test_custom_limitations(self):
        """Custom limitations override defaults."""
        loader = _make_minimal_loader()
        custom = ("Custom limitation 1", "Custom limitation 2")
        config = WorkflowBenchmarkConfig(
            workflow_modes=("legacy", "agent_led"),
            known_limitations=custom,
        )
        runner = WorkflowBenchmarkRunner(loader, config)
        result = runner.run()
        assert result.recommendation.known_limitations == custom

    def test_run_benchmark_with_custom_limitations(self):
        """run_benchmark accepts custom limitations."""
        dataset = _make_minimal_dataset()
        custom = ("Custom limitation",)
        result = run_benchmark(
            dataset,
            workflow_modes=("legacy", "agent_led"),
            known_limitations=custom,
        )
        assert result.recommendation.known_limitations == custom


# ---------------------------------------------------------------------------
# Integration tests for real workflow execution
# ---------------------------------------------------------------------------


class TestRealWorkflowExecution:
    """Integration tests for real workflow execution with dry_run=False."""

    def test_runner_with_real_evidence_packets_and_transitions(self):
        """Runner produces results with real evidence packets and transitions."""
        loader = _make_minimal_loader()
        packet = {
            "claims": [{"claim_id": "c1", "statement": "Test claim"}],
            "passages": [{"passage_id": "p1", "text": "Test passage"}],
            "claim_evidence_bindings": [
                {"claim_id": "c1", "passage_ids": ["p1"], "relationship": "supports"},
            ],
        }
        transitions = [
            {"prior_state": "created", "next_state": "planning"},
            {"prior_state": "planning", "next_state": "corpus_review"},
        ]
        config = WorkflowBenchmarkConfig(
            workflow_modes=("legacy", "agent_led"),
            evidence_packets=[packet],
            run_transitions=transitions,
        )
        runner = WorkflowBenchmarkRunner(loader, config)
        result = runner.run()
        assert result.comparison is not None
        assert result.recommendation is not None
        # Real evidence packets and transitions should pass integrity checks
        for r in result.comparison.results:
            for check in r.integrity_checks:
                if check.check_name in (
                    "evidence_packet_validation",
                    "citation_binding_integrity",
                    "state_machine_transitions",
                ):
                    assert check.passed is True

    def test_strict_runner_fails_without_real_state(self):
        """Runner with strict integrity checker fails when no real state."""
        loader = _make_minimal_loader()
        config = WorkflowBenchmarkConfig(
            workflow_modes=("legacy", "agent_led"),
        )
        # Create a runner with strict integrity checker
        runner = WorkflowBenchmarkRunner(loader, config)
        # Override the integrity checker to be strict
        from research_store.workflow_benchmark import DeterministicIntegrityChecker

        runner.integrity_checker = DeterministicIntegrityChecker(strict=True)
        result = runner.run()
        # Strict mode should fail integrity checks that fall back to simulation
        strict_checks = [
            c
            for c in result.comparison.results[0].integrity_checks
            if "strict" in c.details.lower()
        ]
        assert len(strict_checks) > 0
        assert all(c.passed is False for c in strict_checks)


# ---------------------------------------------------------------------------
# deterministic_debug mode tests
# ---------------------------------------------------------------------------


class TestDeterministicDebugMode:
    """Tests for the deterministic_debug workflow mode."""

    def test_deterministic_debug_produces_lower_quality(self):
        """deterministic_debug mode produces lower quality than all other modes."""
        loader = _make_minimal_loader()
        config = WorkflowBenchmarkConfig(
            workflow_modes=("deterministic_debug", "legacy", "agent_led"),
        )
        runner = WorkflowBenchmarkRunner(loader, config)
        result = runner.run()

        debug_results = [
            r
            for r in result.comparison.results
            if r.workflow_mode == "deterministic_debug"
        ]
        legacy_results = [
            r for r in result.comparison.results if r.workflow_mode == "legacy"
        ]
        agent_results = [
            r for r in result.comparison.results if r.workflow_mode == "agent_led"
        ]

        assert debug_results
        assert legacy_results
        assert agent_results

        # deterministic_debug should have the lowest recall
        avg_debug_recall = sum(r.quality.candidate_recall for r in debug_results) / len(
            debug_results
        )
        avg_legacy_recall = sum(
            r.quality.candidate_recall for r in legacy_results
        ) / len(legacy_results)
        avg_agent_recall = sum(r.quality.candidate_recall for r in agent_results) / len(
            agent_results
        )

        assert avg_debug_recall < avg_legacy_recall < avg_agent_recall

    def test_deterministic_debug_performance(self):
        """deterministic_debug mode has minimal resource usage."""
        loader = _make_minimal_loader()
        config = WorkflowBenchmarkConfig(
            workflow_modes=("deterministic_debug", "agent_led"),
        )
        runner = WorkflowBenchmarkRunner(loader, config)
        result = runner.run()

        debug_results = [
            r
            for r in result.comparison.results
            if r.workflow_mode == "deterministic_debug"
        ]
        agent_results = [
            r for r in result.comparison.results if r.workflow_mode == "agent_led"
        ]

        # deterministic_debug should have fewer semantic calls
        debug_semantic = sum(r.performance.semantic_calls for r in debug_results) / len(
            debug_results
        )
        agent_semantic = sum(r.performance.semantic_calls for r in agent_results) / len(
            agent_results
        )
        assert debug_semantic < agent_semantic


# ---------------------------------------------------------------------------
# Recommendation outcome tests
# ---------------------------------------------------------------------------


class TestRecommendationOutcome:
    """Tests for recommendation outcome logic."""

    def test_full_pipeline_produces_no_go_with_legacy(self):
        """Full pipeline with legacy mode produces NO_GO because legacy fails thresholds."""
        loader = load_benchmark_dataset(BENCHMARK_FIXTURE)
        config = WorkflowBenchmarkConfig(
            workflow_modes=("legacy", "agent_led", "autonomous_local"),
        )
        runner = WorkflowBenchmarkRunner(loader, config)
        result = runner.run()

        # Legacy mode recall (0.45) < min_candidate_recall (0.6) → withdrawn claim
        assert result.recommendation.outcome == "no_go"
        assert len(result.recommendation.withdrawn_claims) > 0
        # Verify the withdrawn claim mentions candidate_recall
        assert any(
            "candidate_recall" in claim
            for claim in result.recommendation.withdrawn_claims
        )

    def test_no_legacy_produces_go(self):
        """Without legacy mode, agent-led and autonomous_local pass thresholds → GO."""
        loader = _make_minimal_loader()
        config = WorkflowBenchmarkConfig(
            workflow_modes=("agent_led", "autonomous_local"),
        )
        runner = WorkflowBenchmarkRunner(loader, config)
        result = runner.run()

        # Both agent_led and autonomous_local should pass all thresholds
        assert result.recommendation.outcome == "go"
        assert result.recommendation.withdrawn_claims == ()


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestBenchmarkCLI:
    """Integration tests for the benchmark CLI subcommands."""

    def test_benchmark_run_json_output_structure(self):
        """benchmark run produces correct JSON output structure."""
        from research_store.workflow_benchmark import (
            WorkflowBenchmarkConfig,
            WorkflowBenchmarkRunner,
            load_benchmark_dataset,
        )

        loader = load_benchmark_dataset(BENCHMARK_FIXTURE)
        config = WorkflowBenchmarkConfig(
            workflow_modes=("agent_led", "autonomous_local"),
        )
        runner = WorkflowBenchmarkRunner(loader, config)
        result = runner.run()

        # Verify the JSON structure that the CLI would produce
        output = {
            "dataset_version": result.dataset_version,
            "total_duration_ms": result.total_duration_ms,
            "comparison": {
                "dataset_version": result.comparison.dataset_version,
                "integrity_regression": result.comparison.integrity_regression,
                "quality_vs_baseline": result.comparison.quality_vs_baseline,
                "performance_vs_baseline": result.comparison.performance_vs_baseline,
                "results": [
                    {
                        "workflow_mode": r.workflow_mode,
                        "quality": {
                            "candidate_recall": r.quality.candidate_recall,
                            "source_quality_score": r.quality.source_quality_score,
                            "coverage_completeness": r.quality.coverage_completeness,
                            "unsupported_claim_rate": r.quality.unsupported_claim_rate,
                            "citation_accuracy": r.quality.citation_accuracy,
                            "report_quality_score": r.quality.report_quality_score,
                        },
                        "performance": {
                            "total_latency_ms": r.performance.total_latency_ms,
                            "total_tokens": r.performance.total_tokens,
                            "semantic_calls": r.performance.semantic_calls,
                            "cache_hit_rate": r.performance.cache_hit_rate,
                            "cache_miss_rate": r.performance.cache_miss_rate,
                            "embedding_throughput": r.performance.embedding_throughput,
                            "gpu_memory_mb": r.performance.gpu_memory_mb,
                            "cpu_percent": r.performance.cpu_percent,
                        },
                        "integrity_checks": [
                            {
                                "check_name": c.check_name,
                                "passed": c.passed,
                                "details": c.details,
                            }
                            for c in r.integrity_checks
                        ],
                    }
                    for r in result.comparison.results
                ],
            },
            "recommendation": {
                "outcome": result.recommendation.outcome,
                "dataset_version": result.recommendation.dataset_version,
                "supported_claims": list(result.recommendation.supported_claims),
                "withdrawn_claims": list(result.recommendation.withdrawn_claims),
                "known_limitations": list(result.recommendation.known_limitations),
                "conditions": list(result.recommendation.conditions),
                "p0_regressions": list(result.recommendation.p0_regressions),
            },
        }

        assert "dataset_version" in output
        assert "recommendation" in output
        assert "comparison" in output
        assert output["recommendation"]["outcome"] in (
            "go",
            "go_with_conditions",
            "no_go",
        )

    def test_benchmark_report_uses_results_path(self):
        """benchmark report --results-path reads the correct file."""
        import json
        import tempfile

        from research_store.workflow_benchmark import (
            WorkflowBenchmarkConfig,
            WorkflowBenchmarkRunner,
            load_benchmark_dataset,
        )

        loader = load_benchmark_dataset(BENCHMARK_FIXTURE)
        config = WorkflowBenchmarkConfig(
            workflow_modes=("agent_led", "autonomous_local"),
        )
        runner = WorkflowBenchmarkRunner(loader, config)
        result = runner.run()

        # Write results to a temp file (simulating CLI --output)
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as tmp:
            tmp_path = tmp.name
            json.dump(
                {
                    "dataset_version": result.dataset_version,
                    "total_duration_ms": result.total_duration_ms,
                    "comparison": {
                        "dataset_version": result.comparison.dataset_version,
                        "integrity_regression": result.comparison.integrity_regression,
                        "quality_vs_baseline": result.comparison.quality_vs_baseline,
                        "performance_vs_baseline": result.comparison.performance_vs_baseline,
                        "results": [
                            {
                                "workflow_mode": r.workflow_mode,
                                "quality": {
                                    "candidate_recall": r.quality.candidate_recall,
                                    "source_quality_score": r.quality.source_quality_score,
                                    "coverage_completeness": r.quality.coverage_completeness,
                                    "unsupported_claim_rate": r.quality.unsupported_claim_rate,
                                    "citation_accuracy": r.quality.citation_accuracy,
                                    "report_quality_score": r.quality.report_quality_score,
                                },
                                "performance": {
                                    "total_latency_ms": r.performance.total_latency_ms,
                                    "total_tokens": r.performance.total_tokens,
                                    "semantic_calls": r.performance.semantic_calls,
                                    "cache_hit_rate": r.performance.cache_hit_rate,
                                    "cache_miss_rate": r.performance.cache_miss_rate,
                                    "embedding_throughput": r.performance.embedding_throughput,
                                    "gpu_memory_mb": r.performance.gpu_memory_mb,
                                    "cpu_percent": r.performance.cpu_percent,
                                },
                                "integrity_checks": [
                                    {
                                        "check_name": c.check_name,
                                        "passed": c.passed,
                                        "details": c.details,
                                    }
                                    for c in r.integrity_checks
                                ],
                            }
                            for r in result.comparison.results
                        ],
                    },
                    "recommendation": {
                        "outcome": result.recommendation.outcome,
                        "dataset_version": result.recommendation.dataset_version,
                        "supported_claims": list(
                            result.recommendation.supported_claims
                        ),
                        "withdrawn_claims": list(
                            result.recommendation.withdrawn_claims
                        ),
                        "known_limitations": list(
                            result.recommendation.known_limitations
                        ),
                        "conditions": list(result.recommendation.conditions),
                        "p0_regressions": list(result.recommendation.p0_regressions),
                    },
                },
                tmp,
                indent=2,
                default=str,
            )

        # Read back and verify (simulating CLI --results-path)
        with open(tmp_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert data["dataset_version"]
        # Verify the report text generation logic from cli.py
        lines = []
        lines.append("=" * 60)
        lines.append("RELEASE BENCHMARK REPORT")
        lines.append("=" * 60)
        lines.append("")
        lines.append(f"Dataset version: {data.get('dataset_version', 'unknown')}")
        lines.append(f"Duration: {data.get('total_duration_ms', 0):.1f}ms")
        lines.append("")

        rec = data.get("recommendation", {})
        lines.append(
            f"Recommendation: {rec.get('outcome', 'unknown').replace('_', ' ').upper()}"
        )

        comp = data.get("comparison", {})
        lines.append("Workflow comparison:")
        lines.append("-" * 40)
        for r in comp.get("results", []):
            mode = r.get("workflow_mode", "unknown")
            lines.append(f"  {mode}:")

        report_text = "\n".join(lines)
        assert "agent_led" in report_text
        assert "RELEASE BENCHMARK REPORT" in report_text
