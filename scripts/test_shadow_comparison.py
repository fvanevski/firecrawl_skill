"""Tests for the shadow comparison harness.

These tests verify:

* Normal success: loading manifests, comparing objectives, generating reports.
* Invalid input: missing fields, unsupported schema versions, missing manifest.
* Strict schema validation on deserialization.
* Duplicate comparison: deterministic results on replay.
* False-completion prevention: detection of false completion cases.
* Dry-run mode: synthetic results without live policy execution.
* Policy adapters: live policy wrappers for legacy and coverage-led control.
* Divergence filtering: P0/P1/P2 severity filtering across all dimensions.
* Candidate discovery vs extraction choice separation.
* Deterministic integrity functional verification.
* Revision tracking: run_revision and coverage_revision fields.
* Report generation: structured output with recommendations.
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
    fsearch_smart_legacy_policy,
    research_orchestrator_coverage_led_policy,
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
    candidate_urls: tuple[str, ...] | None = None,
    search_revisions: list[list[dict]] | None = None,
    query_plan: list[dict] | None = None,
    strategy_proposals: int = 0,
    error: str | None = None,
    run_revision: int = 1,
    coverage_revision: int = 1,
) -> LegacyResult:
    if query_plan is None:
        query_plan = [
            {"query": "test query 1", "facet": "broad_overview"},
            {"query": "test query 2", "facet": "primary_sources"},
        ]
    if candidate_urls is None:
        candidate_urls = tuple(
            f"https://example.com/cand/{i}" for i in range(candidate_count)
        )
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
        candidate_urls=candidate_urls,
        extracted_urls=extracted_urls,
        search_revisions=search_revisions,
        stop_reason=stop_reason,
        final_state=final_state,
        coverage_status=coverage_status,
        strategy_proposals=strategy_proposals,
        error=error,
        run_revision=run_revision,
        coverage_revision=coverage_revision,
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
    candidate_urls: tuple[str, ...] | None = None,
    search_revisions: list[list[dict]] | None = None,
    query_plan: list[dict] | None = None,
    strategy_proposals: int = 0,
    strategy_decisions: int = 0,
    error: str | None = None,
    run_revision: int = 1,
    coverage_revision: int = 1,
) -> CoverageLedResult:
    if query_plan is None:
        query_plan = [
            {"query": "test query 1", "facet": "broad_overview"},
            {"query": "test query 2", "facet": "primary_sources"},
        ]
    if candidate_urls is None:
        candidate_urls = tuple(
            f"https://example.com/cand/{i}" for i in range(candidate_count)
        )
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
        candidate_urls=candidate_urls,
        extracted_urls=extracted_urls,
        search_revisions=search_revisions,
        stop_reason=stop_reason,
        final_state=final_state,
        coverage_status=coverage_status,
        coverage_items=coverage_items,
        strategy_proposals=strategy_proposals,
        strategy_decisions=strategy_decisions,
        error=error,
        run_revision=run_revision,
        coverage_revision=coverage_revision,
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
# Tests: ShadowComparisonEngine & Policy Adapters
# ===================================================================


class TestShadowComparisonEngine(unittest.TestCase):
    """Tests for the shadow comparison engine and policy adapters."""

    def test_dry_run_comparison(self):
        """Test dry-run comparison without live policy execution."""
        engine = ShadowComparisonEngine()
        objectives = [_make_objective()]
        results = engine.compare(objectives, dry_run=True)
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.objective_id, "test-obj")
        self.assertIsInstance(result.legacy, LegacyResult)
        self.assertIsInstance(result.coverage_led, CoverageLedResult)

    def test_dry_run_deterministic_uuid5_run_id(self):
        """Test that dry-run produces identical UUID5 run_ids across repeated calls."""
        engine = ShadowComparisonEngine()
        objectives = [_make_objective(objective_id="det-uuid")]
        results_a = engine.compare(objectives, dry_run=True)
        results_b = engine.compare(objectives, dry_run=True)
        self.assertEqual(results_a[0].legacy.run_id, results_b[0].legacy.run_id)
        self.assertEqual(results_a[0].coverage_led.run_id, results_b[0].coverage_led.run_id)

    def test_non_dry_run_with_default_policy_adapters(self):
        """Test non-dry-run comparison using default live policy adapters."""
        engine = ShadowComparisonEngine()
        objectives = [_make_objective(objective_id="live-adapter-test")]
        results = engine.compare(objectives, dry_run=False)
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertIsNotNone(result.legacy.query_plan)
        self.assertIsNotNone(result.coverage_led.query_plan)
        self.assertGreater(len(result.legacy.candidate_urls), 0)
        self.assertGreater(len(result.coverage_led.candidate_urls), 0)

    def test_non_dry_run_with_custom_mock_policies(self):
        """Test non-dry-run comparison with custom mock policy callables."""

        def legacy_fn(obj):
            return _make_legacy_result(
                stop_reason="budget_exhausted",
                final_state="partial",
                coverage_status="insufficient",
                run_revision=3,
                coverage_revision=2,
            )

        def coverage_fn(obj):
            return _make_coverage_led_result(
                stop_reason="budget_exhausted",
                final_state="partial",
                coverage_status="insufficient",
                run_revision=4,
                coverage_revision=3,
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
        self.assertEqual(results[0].run_revision, 4)
        self.assertEqual(results[0].coverage_revision, 3)

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
# Tests: Policy Adapters
# ===================================================================


class TestPolicyAdapters(unittest.TestCase):
    """Tests for live policy adapters."""

    def test_fsearch_smart_legacy_policy_adapter(self):
        """Test fsearch_smart legacy policy adapter produces valid LegacyResult."""
        obj = _make_objective(objective_id="adapter-leg", expected_complexity="moderate")
        result = fsearch_smart_legacy_policy(obj)
        self.assertIsInstance(result, LegacyResult)
        self.assertEqual(result.final_state, "completed")
        self.assertEqual(result.coverage_status, "unassessed")
        self.assertGreater(len(result.query_plan), 0)
        self.assertGreater(len(result.candidate_urls), 0)

    def test_research_orchestrator_coverage_led_policy_adapter(self):
        """Test ResearchOrchestrator coverage-led policy adapter produces valid CoverageLedResult."""
        obj = _make_objective(objective_id="adapter-cov", expected_complexity="complex")
        result = research_orchestrator_coverage_led_policy(obj)
        self.assertIsInstance(result, CoverageLedResult)
        self.assertEqual(result.final_state, "completed")
        self.assertEqual(result.coverage_status, "sufficient")
        self.assertGreater(len(result.query_plan), 0)
        self.assertGreater(len(result.candidate_urls), 0)


# ===================================================================
# Tests: Comparison results and divergence detection
# ===================================================================


class TestComparisonResults(unittest.TestCase):
    """Tests for comparison result generation and divergence detection."""

    def test_false_completion_legacy(self):
        """Test false completion detection for legacy policy."""
        legacy = _make_legacy_result(
            stop_reason="page_target_reached",
            final_state="completed",
            coverage_status="unassessed",
        )
        coverage = _make_coverage_led_result(
            stop_reason="coverage_sufficient",
            final_state="completed",
            coverage_status="sufficient",
        )
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)
        false_completions = [
            d for d in divergences if d.dimension == "false_completion"
        ]
        self.assertGreater(len(false_completions), 0)
        self.assertEqual(false_completions[0].severity, "P0")

    def test_candidate_set_divergence_separate_from_extraction_choices(self):
        """Test candidate-set divergence is separate from extraction-choice divergence."""
        legacy = _make_legacy_result(
            candidate_urls=("https://a.com/c1", "https://a.com/c2"),
            extracted_urls=("https://a.com/c1",),
        )
        coverage = _make_coverage_led_result(
            candidate_urls=("https://b.com/c1", "https://b.com/c2"),
            extracted_urls=("https://a.com/c1",),  # Extracted URLs match!
        )
        engine = ShadowComparisonEngine()
        divergences = engine._compare_results(_make_objective(), legacy, coverage)

        candidate_divs = [d for d in divergences if d.dimension == "candidate_set"]
        extraction_divs = [d for d in divergences if d.dimension == "extraction_choices"]

        self.assertEqual(len(candidate_divs), 1)
        self.assertEqual(candidate_divs[0].severity, "P2")
        self.assertEqual(len(extraction_divs), 0)  # Extracted URLs are identical

    def test_extraction_choice_divergence(self):
        """Test extraction-choice divergence detection when scraped URLs differ."""
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
        self.assertEqual(len(extraction_divs), 1)
        self.assertEqual(extraction_divs[0].severity, "P1")

    def test_strict_from_dict_schema_validation(self):
        """Test that missing required keys in ComparisonResult.from_dict raises ValueError."""
        with self.assertRaises(ValueError):
            ComparisonResult.from_dict({})

        with self.assertRaises(ValueError):
            ComparisonResult.from_dict({"objective_id": "test"})

    def test_verify_deterministic_integrity(self):
        """Test functional deterministic_integrity check."""
        engine = ShadowComparisonEngine()
        obj = _make_objective()
        legacy = _make_legacy_result(run_revision=1, coverage_revision=1)
        coverage = _make_coverage_led_result(run_revision=1, coverage_revision=1)

        is_valid = engine._verify_deterministic_integrity(obj, legacy, coverage)
        self.assertTrue(is_valid)

        # Negative wave count breaks integrity
        bad_legacy = _make_legacy_result(wave_count=-1)
        is_invalid = engine._verify_deterministic_integrity(obj, bad_legacy, coverage)
        self.assertFalse(is_invalid)


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

    def test_report_includes_revisions(self):
        """Test report serialized objects include run and coverage revisions."""
        legacy = _make_legacy_result(run_revision=2, coverage_revision=3)
        coverage = _make_coverage_led_result(run_revision=4, coverage_revision=5)
        result = ComparisonResult(
            objective_id="rev-test",
            legacy=legacy,
            coverage_led=coverage,
            run_revision=4,
            coverage_revision=5,
        )
        d = result.to_dict()
        self.assertEqual(d["run_revision"], 4)
        self.assertEqual(d["coverage_revision"], 5)


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

    def test_run_live_adapters_with_manifest(self):
        """Test CLI run command executing live adapters."""
        import io
        import contextlib

        f = io.StringIO()
        with contextlib.redirect_stdout(f):
            code = shadow_comparison.main(
                [
                    "run",
                    "--fixture",
                    str(_MANIFEST_PATH),
                ]
            )
        self.assertEqual(code, 0)
        output = json.loads(f.getvalue())
        self.assertEqual(output["schema_version"], "shadow-comparison-report-v1")
        self.assertEqual(output["objective_count"], 5)


if __name__ == "__main__":
    unittest.main()
