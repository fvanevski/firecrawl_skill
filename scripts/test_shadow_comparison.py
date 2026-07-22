"""Tests for the shadow comparison harness.

These tests verify:

* Normal success: loading manifests, comparing objectives, generating reports.
* Invalid input: missing fields, unsupported schema versions, missing manifest.
* Duplicate comparison: deterministic results on replay.
* False-completion prevention: detection of false completion cases.
* Dry-run mode: synthetic results without real policy execution.
* Divergence filtering: P0/P1/P2 severity filtering.
* Report generation: structured output with recommendations.
* Compatibility: no network or database required for dry-run.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from uuid import uuid4

# Ensure scripts/ is on the path so imports resolve.
_SCRIPT_DIR = __file__.rsplit("/", 1)[0] or "."

if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import shadow_comparison  # noqa: E402
from shadow_comparison import (  # noqa: E402,F401
    BenchmarkObjective,
    ComparisonResult,
    Divergence,
    LegacyResult,
    CoverageLedResult,
    ShadowComparisonEngine,
    generate_report,
    list_divergences,
)


# ===================================================================
# Fixtures
# ===================================================================


_FIXTURE_DIR = Path(__file__).parents[1] / "tests" / "fixtures" / "shadow_comparison"
_MANIFEST_PATH = _FIXTURE_DIR / "manifest.json"


def _make_legacy_result(
    stop_reason: str = "page_target_reached",
    final_state: str = "completed",
    coverage_status: str = "unassessed",
    wave_count: int = 1,
    candidate_count: int = 10,
    successful_extractions: int = 4,
    extracted_urls: tuple[str, ...] | None = None,
    search_revisions: list[list[dict]] | None = None,
    query_plan: list[dict] | None = None,
    strategy_proposals: int = 0,
    error: str | None = None,
) -> LegacyResult:
    if query_plan is None:
        query_plan = [
            {"query": "test query 1", "facet": "broad_overview"},
            {"query": "test query 2", "facet": "primary_sources"},
        ]
    if extracted_urls is None:
        extracted_urls = tuple(
            f"https://example.com/page/{i}" for i in range(successful_extractions)
        )
    if search_revisions is None:
        search_revisions = [query_plan]
    return LegacyResult(
        run_id=str(uuid4()),
        query_plan=query_plan,
        wave_count=wave_count,
        candidate_count=candidate_count,
        successful_extractions=successful_extractions,
        extracted_urls=extracted_urls,
        search_revisions=search_revisions,
        stop_reason=stop_reason,
        final_state=final_state,
        coverage_status=coverage_status,
        strategy_proposals=strategy_proposals,
        error=error,
    )


def _make_coverage_led_result(
    stop_reason: str = "coverage_sufficient",
    final_state: str = "completed",
    coverage_status: str = "sufficient",
    wave_count: int = 1,
    candidate_count: int = 10,
    successful_extractions: int = 4,
    coverage_items: int = 2,
    extracted_urls: tuple[str, ...] | None = None,
    search_revisions: list[list[dict]] | None = None,
    query_plan: list[dict] | None = None,
    strategy_proposals: int = 0,
    strategy_decisions: int = 0,
    error: str | None = None,
) -> CoverageLedResult:
    if query_plan is None:
        query_plan = [
            {"query": "test query 1", "facet": "broad_overview"},
            {"query": "test query 2", "facet": "primary_sources"},
        ]
    if extracted_urls is None:
        extracted_urls = tuple(
            f"https://example.com/source/{i}" for i in range(successful_extractions)
        )
    if search_revisions is None:
        search_revisions = [query_plan]
    return CoverageLedResult(
        run_id=str(uuid4()),
        query_plan=query_plan,
        wave_count=wave_count,
        candidate_count=candidate_count,
        successful_extractions=successful_extractions,
        extracted_urls=extracted_urls,
        search_revisions=search_revisions,
        stop_reason=stop_reason,
        final_state=final_state,
        coverage_status=coverage_status,
        coverage_items=coverage_items,
        strategy_proposals=strategy_proposals,
        strategy_decisions=strategy_decisions,
        error=error,
    )


def _make_objective(
    objective_id: str = "test-obj",
    objective: str = "Test research objective",
    research_archetype: str = "general",
    expected_complexity: str = "moderate",
    expected_behavior: str = "",
) -> BenchmarkObjective:
    return BenchmarkObjective(
        objective_id=objective_id,
        objective=objective,
        research_archetype=research_archetype,
        expected_complexity=expected_complexity,
        expected_behavior=expected_behavior,
    )


# ===================================================================
# Tests: BenchmarkObjective
# ===================================================================


class TestBenchmarkObjective(unittest.TestCase):
    """Tests for BenchmarkObjective loading and validation."""

    def test_load_manifest(self):
        """Test loading benchmark objectives from manifest."""
        objectives = BenchmarkObjective.load_manifest(_MANIFEST_PATH)
        self.assertGreater(len(objectives), 0)
        for obj in objectives:
            self.assertIsInstance(obj, BenchmarkObjective)
            self.assertTrue(obj.objective_id.startswith("obj-"))

    def test_load_manifest_missing_file(self):
        """Test that missing manifest raises FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            BenchmarkObjective.load_manifest("/nonexistent/manifest.json")

    def test_load_manifest_unsupported_schema(self):
        """Test unsupported schema version raises ValueError."""
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"schema_version": "unsupported-v2", "objectives": []}, f)
            f.flush()
            with self.assertRaises(ValueError):
                BenchmarkObjective.load_manifest(f.name)

    def test_from_dict_missing_fields(self):
        """Test that missing required fields raises ValueError."""
        with self.assertRaises(ValueError):
            BenchmarkObjective.from_dict({"objective_id": "test"})

    def test_from_dict_valid(self):
        """Test valid from_dict conversion."""
        data = {
            "objective_id": "test-1",
            "objective": "Test objective",
            "research_archetype": "general",
            "expected_complexity": "simple",
        }
        obj = BenchmarkObjective.from_dict(data)
        self.assertEqual(obj.objective_id, "test-1")
        self.assertEqual(obj.expected_complexity, "simple")

    def test_manifest_content(self):
        """Test that the manifest has expected objectives."""
        objectives = BenchmarkObjective.load_manifest(_MANIFEST_PATH)
        ids = [obj.objective_id for obj in objectives]
        self.assertIn("obj-simple-focused", ids)
        self.assertIn("obj-complex-risk", ids)
        self.assertIn("obj-false-completion-risk", ids)


# ===================================================================
# Tests: ShadowComparisonEngine
# ===================================================================


class TestShadowComparisonEngine(unittest.TestCase):
    """Tests for the shadow comparison engine."""

    def test_dry_run_comparison(self):
        """Test dry-run comparison without real policy execution."""
        engine = ShadowComparisonEngine()
        objectives = [_make_objective()]
        results = engine.compare(objectives, dry_run=True)
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.objective_id, "test-obj")
        self.assertIsInstance(result.legacy, LegacyResult)
        self.assertIsInstance(result.coverage_led, CoverageLedResult)

    def test_dry_run_produces_deterministic_structure(self):
        """Test that dry-run produces consistent structure."""
        engine = ShadowComparisonEngine()
        objectives = [_make_objective(objective_id="det-test")]
        results = engine.compare(objectives, dry_run=True)
        self.assertTrue(results[0].deterministic_integrity)

    def test_non_dry_run_requires_policies(self):
        """Test that non-dry-run raises error without policy implementations."""
        engine = ShadowComparisonEngine()
        objectives = [_make_objective()]
        with self.assertRaises(ValueError):
            engine.compare(objectives, dry_run=False)

    def test_non_dry_run_with_mock_policies(self):
        """Test non-dry-run comparison with mock policy callables."""

        def legacy_fn(obj):
            return _make_legacy_result(
                stop_reason="budget_exhausted",
                final_state="partial",
                coverage_status="insufficient",
            )

        def coverage_fn(obj):
            return _make_coverage_led_result(
                stop_reason="budget_exhausted",
                final_state="partial",
                coverage_status="insufficient",
            )

        engine = ShadowComparisonEngine(
            legacy_policy=legacy_fn,
            coverage_led_policy=coverage_fn,
        )
        objectives = [_make_objective()]
        results = engine.compare(objectives, dry_run=False)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].legacy.final_state, "partial")
        self.assertEqual(results[0].coverage_led.final_state, "partial")

    def test_compare_multiple_objectives(self):
        """Test comparison across multiple objectives."""
        engine = ShadowComparisonEngine()
        objectives = [_make_objective(objective_id=f"obj-{i}") for i in range(3)]
        results = engine.compare(objectives, dry_run=True)
        self.assertEqual(len(results), 3)
        ids = [r.objective_id for r in results]
        self.assertEqual(set(ids), {"obj-0", "obj-1", "obj-2"})

    def test_compare_uses_manifest(self):
        """Test comparison using the actual manifest file."""
        engine = ShadowComparisonEngine()
        objectives = BenchmarkObjective.load_manifest(_MANIFEST_PATH)
        results = engine.compare(objectives, dry_run=True)
        self.assertEqual(len(results), len(objectives))


# ===================================================================
# Tests: Comparison results and divergence detection
# ===================================================================


class TestComparisonResults(unittest.TestCase):
    """Tests for comparison result generation and divergence detection."""

    def test_identical_results_no_divergences(self):
        """Test that identical results produce minimal divergences."""
        obj = _make_objective()
        engine = ShadowComparisonEngine()
        # Use dry_run to avoid needing policy implementations
        result = engine._compare_objective(obj, dry_run=True)
        self.assertIsInstance(result, ComparisonResult)

    def test_false_completion_legacy(self):
        """Test false completion detection for legacy policy."""
        legacy = _make_legacy_result(
            stop_reason="page_target_reached",
            final_state="completed",
            coverage_status="unassessed",  # Not sufficient
        )
        coverage = _make_coverage_led_result(
            stop_reason="coverage_sufficient",
            final_state="completed",
            coverage_status="sufficient",
        )
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        # Should detect false completion in legacy
        false_completions = [
            d for d in divergences if d.dimension == "false_completion"
        ]
        self.assertGreater(len(false_completions), 0)
        self.assertEqual(false_completions[0].severity, "P0")

    def test_false_completion_coverage_led(self):
        """Test false completion detection for coverage-led policy."""
        legacy = _make_legacy_result(
            stop_reason="page_target_reached",
            final_state="completed",
            coverage_status="sufficient",
        )
        coverage = _make_coverage_led_result(
            stop_reason="page_target_reached",
            final_state="completed",
            coverage_status="insufficient",  # Not sufficient
        )
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        false_completions = [
            d for d in divergences if d.dimension == "false_completion"
        ]
        self.assertGreater(len(false_completions), 0)
        self.assertEqual(false_completions[0].severity, "P0")

    def test_stop_decision_divergence_p0(self):
        """Test P0 severity for stop decision divergence on completion."""
        legacy = _make_legacy_result(
            final_state="completed",
            coverage_status="sufficient",
        )
        coverage = _make_coverage_led_result(
            final_state="partial",
            coverage_status="insufficient",
        )
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        stop_divs = [d for d in divergences if d.dimension == "stop_decision"]
        self.assertGreater(len(stop_divs), 0)
        self.assertEqual(stop_divs[0].severity, "P0")

    def test_query_plan_divergence_p2(self):
        """Test P2 severity for query plan divergence."""
        legacy = _make_legacy_result(
            query_plan=[{"query": "legacy query", "facet": "broad"}],
        )
        coverage = _make_coverage_led_result(
            query_plan=[{"query": "coverage query", "facet": "adaptive"}],
        )
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        query_divs = [d for d in divergences if d.dimension == "query_plan"]
        self.assertGreater(len(query_divs), 0)
        self.assertEqual(query_divs[0].severity, "P2")

    def test_wave_count_divergence_p1(self):
        """Test P1 severity for wave count divergence."""
        legacy = _make_legacy_result(wave_count=3)
        coverage = _make_coverage_led_result(wave_count=1)
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        wave_divs = [d for d in divergences if d.dimension == "wave_count"]
        self.assertGreater(len(wave_divs), 0)
        self.assertEqual(wave_divs[0].severity, "P1")

    def test_comparison_result_to_dict(self):
        """Test ComparisonResult serialization."""
        legacy = _make_legacy_result()
        coverage = _make_coverage_led_result()
        result = ComparisonResult(
            objective_id="test",
            legacy=legacy,
            coverage_led=coverage,
            divergences=[],
        )
        d = result.to_dict()
        self.assertEqual(d["objective_id"], "test")
        self.assertIn("legacy", d)
        self.assertIn("coverage_led", d)
        self.assertIn("divergences", d)

    def test_extraction_choice_divergence(self):
        """Test extraction-choice divergence detection."""
        legacy = _make_legacy_result(
            extracted_urls=("https://a.com/1", "https://a.com/2"),
        )
        coverage = _make_coverage_led_result(
            extracted_urls=("https://b.com/1", "https://b.com/2"),
        )
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        extraction_divs = [
            d for d in divergences if d.dimension == "extraction_choices"
        ]
        self.assertGreater(len(extraction_divs), 0)
        self.assertEqual(extraction_divs[0].severity, "P1")

    def test_candidate_set_divergence(self):
        """Test candidate-set (URL-level) divergence detection."""
        legacy = _make_legacy_result(
            extracted_urls=("https://a.com/1", "https://a.com/2"),
        )
        coverage = _make_coverage_led_result(
            extracted_urls=("https://a.com/1", "https://c.com/1"),
        )
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        set_divs = [d for d in divergences if d.dimension == "candidate_set"]
        self.assertGreater(len(set_divs), 0)
        self.assertEqual(set_divs[0].severity, "P1")

    def test_search_revision_divergence(self):
        """Test search-revision divergence detection."""
        legacy = _make_legacy_result(
            search_revisions=[[{"query": "q1"}]],
        )
        coverage = _make_coverage_led_result(
            search_revisions=[[{"query": "q1"}], [{"query": "q2"}]],
        )
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        rev_divs = [d for d in divergences if d.dimension == "search_revisions"]
        self.assertGreater(len(rev_divs), 0)
        self.assertEqual(rev_divs[0].severity, "P2")

    def test_identical_urls_no_extraction_divergence(self):
        """Test that identical extracted URLs produce no divergence."""
        urls = ("https://a.com/1", "https://a.com/2")
        legacy = _make_legacy_result(extracted_urls=urls)
        coverage = _make_coverage_led_result(extracted_urls=urls)
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        extraction_divs = [
            d for d in divergences if d.dimension == "extraction_choices"
        ]
        self.assertEqual(len(extraction_divs), 0)


# ===================================================================
# Tests: Report generation
# ===================================================================


class TestReportGeneration(unittest.TestCase):
    """Tests for comparison report generation."""

    def test_report_with_no_divergences(self):
        """Test report generation with clean results."""
        legacy = _make_legacy_result(
            query_plan=[{"query": "q1"}],
            stop_reason="page_target",
            final_state="completed",
            coverage_status="sufficient",
        )
        coverage = _make_coverage_led_result(
            query_plan=[{"query": "q1"}],
            stop_reason="coverage_sufficient",
            final_state="completed",
            coverage_status="sufficient",
        )
        result = ComparisonResult(
            objective_id="clean",
            legacy=legacy,
            coverage_led=coverage,
            divergences=[],
        )
        report = generate_report([result])
        self.assertEqual(report["schema_version"], "shadow-comparison-report-v1")
        self.assertEqual(report["objective_count"], 1)
        self.assertEqual(report["p0_divergences"], 0)
        self.assertEqual(report["false_completion_cases"], 0)
        self.assertEqual(report["recommendation"], "approve")

    def test_report_with_p0_divergence(self):
        """Test report with P0 divergence triggers further_review."""
        legacy = _make_legacy_result(
            final_state="completed",
            coverage_status="unassessed",
        )
        coverage = _make_coverage_led_result(
            final_state="partial",
            coverage_status="insufficient",
        )
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        result = ComparisonResult(
            objective_id="p0-test",
            legacy=legacy,
            coverage_led=coverage,
            divergences=divergences,
        )
        report = generate_report([result])
        self.assertGreaterEqual(report["p0_divergences"], 1)
        self.assertEqual(report["recommendation"], "further_review_required")

    def test_report_with_false_completion(self):
        """Test report with false completion case."""
        legacy = _make_legacy_result(
            final_state="completed",
            coverage_status="unassessed",
        )
        coverage = _make_coverage_led_result(
            final_state="partial",
            coverage_status="insufficient",
        )
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        result = ComparisonResult(
            objective_id="false-comp",
            legacy=legacy,
            coverage_led=coverage,
            divergences=divergences,
            false_completion_legacy=True,
        )
        report = generate_report([result])
        self.assertEqual(report["false_completion_cases"], 1)
        self.assertEqual(report["recommendation"], "further_review_required")

    def test_report_written_to_file(self):
        """Test that report can be written to a file."""
        import tempfile

        legacy = _make_legacy_result()
        coverage = _make_coverage_led_result()
        result = ComparisonResult(
            objective_id="file-test",
            legacy=legacy,
            coverage_led=coverage,
        )
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            output_path = f.name
        generate_report([result], output_path=output_path)
        self.assertTrue(Path(output_path).exists())
        written = json.loads(Path(output_path).read_text(encoding="utf-8"))
        self.assertEqual(written["objective_count"], 1)

    def test_report_deterministic_integrity(self):
        """Test deterministic integrity flag."""
        legacy = _make_legacy_result()
        coverage = _make_coverage_led_result()
        result = ComparisonResult(
            objective_id="integrity-test",
            legacy=legacy,
            coverage_led=coverage,
            deterministic_integrity=True,
        )
        report = generate_report([result])
        self.assertTrue(report["deterministic_integrity"])

    def test_report_multiple_objectives(self):
        """Test report across multiple objectives."""
        results = []
        for i in range(3):
            legacy = _make_legacy_result(
                query_plan=[{"query": f"q{i}"}],
            )
            coverage = _make_coverage_led_result(
                query_plan=[{"query": f"q{i}"}],
            )
            results.append(
                ComparisonResult(
                    objective_id=f"obj-{i}",
                    legacy=legacy,
                    coverage_led=coverage,
                )
            )
        report = generate_report(results)
        self.assertEqual(report["objective_count"], 3)


# ===================================================================
# Tests: Divergence filtering
# ===================================================================


class TestDivergenceFiltering(unittest.TestCase):
    """Tests for divergence listing and filtering."""

    def test_list_all_divergences(self):
        """Test listing all divergences without filter."""
        legacy = _make_legacy_result(
            query_plan=[{"query": "q1"}],
            wave_count=3,
        )
        coverage = _make_coverage_led_result(
            query_plan=[{"query": "q2"}],
            wave_count=1,
        )
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        self.assertGreater(len(divergences), 0)

    def test_filter_p0_divergences(self):
        """Test filtering divergences by P0 severity."""
        legacy = _make_legacy_result(
            final_state="completed",
            coverage_status="unassessed",
        )
        coverage = _make_coverage_led_result(
            final_state="partial",
            coverage_status="insufficient",
        )
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        p0_only = [d for d in divergences if d.severity == "P0"]
        self.assertGreater(len(p0_only), 0)

    def test_filter_p2_divergences(self):
        """Test filtering divergences by P2 severity."""
        legacy = _make_legacy_result(
            query_plan=[{"query": "q1"}],
        )
        coverage = _make_coverage_led_result(
            query_plan=[{"query": "q2"}],
        )
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        p2_only = [d for d in divergences if d.severity == "P2"]
        self.assertGreater(len(p2_only), 0)


# ===================================================================
# Tests: Idempotency and deterministic replay
# ===================================================================


class TestDeterministicReplay(unittest.TestCase):
    """Tests for deterministic behavior and replay."""

    def test_dry_run_replay_produces_same_structure(self):
        """Test that replay produces the same result structure."""
        engine = ShadowComparisonEngine()
        objectives = [_make_objective(objective_id="replay-test")]
        results_a = engine.compare(objectives, dry_run=True)
        results_b = engine.compare(objectives, dry_run=True)
        # Same number of results
        self.assertEqual(len(results_a), len(results_b))
        # Same objective ID
        self.assertEqual(results_a[0].objective_id, results_b[0].objective_id)
        # Same stop reasons (synthetic)
        self.assertEqual(
            results_a[0].legacy.stop_reason, results_b[0].legacy.stop_reason
        )

    def test_manifest_load_is_deterministic(self):
        """Test that manifest loading is deterministic."""
        objs_a = BenchmarkObjective.load_manifest(_MANIFEST_PATH)
        objs_b = BenchmarkObjective.load_manifest(_MANIFEST_PATH)
        self.assertEqual(len(objs_a), len(objs_b))
        for a, b in zip(objs_a, objs_b):
            self.assertEqual(a.objective_id, b.objective_id)
            self.assertEqual(a.objective, b.objective)


# ===================================================================
# Tests: CLI entry point
# ===================================================================


class TestCLI(unittest.TestCase):
    """Tests for the shadow_comparison CLI entry point."""

    def test_no_command_shows_help(self):
        """Test that no command prints help and returns 1."""
        import io
        import contextlib

        f = io.StringIO()
        with contextlib.redirect_stdout(f):
            code = shadow_comparison.main([])
        self.assertEqual(code, 1)

    def test_run_dry_run_with_manifest(self):
        """Test CLI run command with dry-run."""
        import io
        import contextlib

        f = io.StringIO()
        with contextlib.redirect_stdout(f):
            code = shadow_comparison.main(
                [
                    "run",
                    "--fixture",
                    str(_MANIFEST_PATH),
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 0)
        output = json.loads(f.getvalue())
        self.assertEqual(output["schema_version"], "shadow-comparison-report-v1")
        self.assertEqual(output["objective_count"], 5)

    def test_run_with_output_file(self):
        """Test CLI run command with output file."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            output_path = f.name

        code = shadow_comparison.main(
            [
                "run",
                "--fixture",
                str(_MANIFEST_PATH),
                "--dry-run",
                "--output",
                output_path,
            ]
        )
        self.assertEqual(code, 0)
        self.assertTrue(Path(output_path).exists())

    def test_report_command(self):
        """Test CLI report command."""
        import tempfile

        # First generate a comparison results file
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            input_path = f.name
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            output_path = f.name

        # Create a minimal comparison results file
        data = {
            "objectives": [
                {
                    "objective_id": "test",
                    "legacy": {
                        "run_id": str(uuid4()),
                        "query_plan": [{"query": "q1"}],
                        "wave_count": 1,
                        "candidate_count": 5,
                        "successful_extractions": 2,
                        "stop_reason": "page_target",
                        "final_state": "completed",
                        "coverage_status": "sufficient",
                        "strategy_proposals": 0,
                        "error": None,
                        "wall_clock_seconds": 0.0,
                    },
                    "coverage_led": {
                        "run_id": str(uuid4()),
                        "query_plan": [{"query": "q1"}],
                        "wave_count": 1,
                        "candidate_count": 5,
                        "successful_extractions": 2,
                        "stop_reason": "coverage_sufficient",
                        "final_state": "completed",
                        "coverage_status": "sufficient",
                        "coverage_items": 1,
                        "strategy_proposals": 0,
                        "strategy_decisions": 0,
                        "error": None,
                        "wall_clock_seconds": 0.0,
                    },
                    "divergences": [],
                    "false_completion_legacy": False,
                    "false_completion_coverage_led": False,
                    "deterministic_integrity": True,
                }
            ]
        }
        Path(input_path).write_text(json.dumps(data), encoding="utf-8")

        code = shadow_comparison.main(
            ["report", "--input", input_path, "--output", output_path]
        )
        self.assertEqual(code, 0)
        self.assertTrue(Path(output_path).exists())

    def test_divergences_command(self):
        """Test CLI divergences command."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            input_path = f.name

        # Create a comparison results file with divergences
        data = {
            "objectives": [
                {
                    "objective_id": "test",
                    "legacy": {
                        "run_id": str(uuid4()),
                        "query_plan": [{"query": "q1"}],
                        "wave_count": 3,
                        "candidate_count": 10,
                        "successful_extractions": 4,
                        "stop_reason": "page_target",
                        "final_state": "completed",
                        "coverage_status": "unassessed",
                        "strategy_proposals": 0,
                        "error": None,
                        "wall_clock_seconds": 0.0,
                    },
                    "coverage_led": {
                        "run_id": str(uuid4()),
                        "query_plan": [{"query": "q2"}],
                        "wave_count": 1,
                        "candidate_count": 5,
                        "successful_extractions": 2,
                        "stop_reason": "coverage_sufficient",
                        "final_state": "completed",
                        "coverage_status": "sufficient",
                        "coverage_items": 1,
                        "strategy_proposals": 0,
                        "strategy_decisions": 0,
                        "error": None,
                        "wall_clock_seconds": 0.0,
                    },
                    "divergences": [
                        {
                            "dimension": "query_plan",
                            "severity": "P2",
                            "legacy_value": ["q1"],
                            "coverage_led_value": ["q2"],
                            "explanation": "test",
                            "resolved": False,
                        },
                        {
                            "dimension": "stop_decision",
                            "severity": "P0",
                            "legacy_value": {
                                "reason": "page_target",
                                "state": "completed",
                            },
                            "coverage_led_value": {
                                "reason": "coverage_sufficient",
                                "state": "completed",
                            },
                            "explanation": "test",
                            "resolved": False,
                        },
                    ],
                    "false_completion_legacy": True,
                    "false_completion_coverage_led": False,
                    "deterministic_integrity": True,
                }
            ]
        }
        Path(input_path).write_text(json.dumps(data), encoding="utf-8")

        # Test listing P0 divergences
        code = shadow_comparison.main(
            ["divergences", "--input", input_path, "--level", "P0"]
        )
        self.assertEqual(code, 0)

    def test_unsupported_command(self):
        """Test that unsupported commands raise SystemExit (argparse behavior)."""
        with self.assertRaises(SystemExit):
            shadow_comparison.main(["unsupported"])


# ===================================================================
# Tests: Result serialization
# ===================================================================


class TestResultSerialization(unittest.TestCase):
    """Tests for LegacyResult and CoverageLedResult serialization."""

    def test_legacy_result_to_dict(self):
        """Test LegacyResult.to_dict() includes all fields."""
        result = _make_legacy_result()
        d = result.to_dict()
        self.assertIn("run_id", d)
        self.assertIn("query_plan", d)
        self.assertIn("wave_count", d)
        self.assertIn("candidate_count", d)
        self.assertIn("successful_extractions", d)
        self.assertIn("extracted_urls", d)
        self.assertIn("search_revisions", d)
        self.assertIn("stop_reason", d)
        self.assertIn("final_state", d)
        self.assertIn("coverage_status", d)
        self.assertIn("strategy_proposals", d)
        self.assertIn("error", d)
        self.assertIn("wall_clock_seconds", d)

    def test_coverage_led_result_to_dict(self):
        """Test CoverageLedResult.to_dict() includes all fields."""
        result = _make_coverage_led_result()
        d = result.to_dict()
        self.assertIn("run_id", d)
        self.assertIn("query_plan", d)
        self.assertIn("wave_count", d)
        self.assertIn("candidate_count", d)
        self.assertIn("successful_extractions", d)
        self.assertIn("extracted_urls", d)
        self.assertIn("search_revisions", d)
        self.assertIn("stop_reason", d)
        self.assertIn("final_state", d)
        self.assertIn("coverage_status", d)
        self.assertIn("coverage_items", d)
        self.assertIn("strategy_proposals", d)
        self.assertIn("strategy_decisions", d)
        self.assertIn("error", d)
        self.assertIn("wall_clock_seconds", d)

    def test_round_trip_serialization(self):
        """Test that to_dict/from_dict round-trip preserves data."""
        legacy = _make_legacy_result(
            extracted_urls=("https://a.com/1", "https://a.com/2"),
            search_revisions=[[{"query": "q1"}, {"query": "q2"}]],
        )
        coverage = _make_coverage_led_result(
            extracted_urls=("https://b.com/1", "https://b.com/2"),
            search_revisions=[[{"query": "q1"}]],
        )
        result = ComparisonResult(
            objective_id="roundtrip",
            legacy=legacy,
            coverage_led=coverage,
        )
        d = result.to_dict()
        restored = ComparisonResult.from_dict(d)
        self.assertEqual(restored.objective_id, "roundtrip")
        self.assertEqual(restored.legacy.extracted_urls, legacy.extracted_urls)
        self.assertEqual(restored.legacy.search_revisions, legacy.search_revisions)
        self.assertEqual(restored.coverage_led.extracted_urls, coverage.extracted_urls)

    def test_divergence_to_dict(self):
        """Test Divergence.to_dict() includes all fields."""
        div = Divergence(
            dimension="test",
            severity="P1",
            legacy_value="a",
            coverage_led_value="b",
            explanation="test divergence",
            resolved=False,
        )
        d = div.to_dict()
        self.assertEqual(d["dimension"], "test")
        self.assertEqual(d["severity"], "P1")
        self.assertFalse(d["resolved"])


# ===================================================================
# Tests: Edge cases
# ===================================================================


class TestEdgeCases(unittest.TestCase):
    """Tests for edge cases and boundary conditions."""

    def test_empty_objective_list(self):
        """Test comparison with empty objective list."""
        engine = ShadowComparisonEngine()
        results = engine.compare([], dry_run=True)
        self.assertEqual(len(results), 0)

    def test_legacy_with_error(self):
        """Test handling of legacy result with error."""
        legacy = _make_legacy_result(error="network timeout")
        coverage = _make_coverage_led_result()
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        self.assertIsInstance(divergences, list)

    def test_coverage_led_with_error(self):
        """Test handling of coverage-led result with error."""
        legacy = _make_legacy_result()
        coverage = _make_coverage_led_result(error="database connection failed")
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        self.assertIsInstance(divergences, list)

    def test_zero_wave_count(self):
        """Test comparison with zero wave count."""
        legacy = _make_legacy_result(wave_count=0, candidate_count=0)
        coverage = _make_coverage_led_result(wave_count=0, candidate_count=0)
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        self.assertIsInstance(divergences, list)

    def test_high_strategy_proposal_count(self):
        """Test comparison with high strategy proposal count."""
        legacy = _make_legacy_result(strategy_proposals=0)
        coverage = _make_coverage_led_result(
            strategy_proposals=10, strategy_decisions=8
        )
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        strategy_divs = [d for d in divergences if d.dimension == "strategy_proposals"]
        self.assertGreater(len(strategy_divs), 0)


if __name__ == "__main__":
    unittest.main()
