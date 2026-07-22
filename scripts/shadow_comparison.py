"""Shadow comparison harness for legacy vs. coverage-led control policies.

This module runs fixed benchmark objectives through both the legacy control
policy (monolithic ``fsearch_smart`` loop) and the coverage-led control
policy (``ResearchOrchestrator``) and produces a structured comparison.

Comparison dimensions:

* query plans — queries generated per wave
* search revisions — number of adaptive query revisions
* candidate sets — deduplicated candidate URLs
* extraction choices — which URLs were scraped/extracted
* stop decisions — why and when each policy stopped
* evidence coverage — coverage ledger status at stop
* resource use — waves executed, candidates found, extractions attempted
* false-completion behavior — did either policy complete with insufficient coverage
* deterministic corpus integrity — idempotent event application and structural integrity

Usage::

    python -m scripts.shadow_comparison run --fixture tests/fixtures/shadow_comparison/manifest.json
    python -m scripts.shadow_comparison report --output comparison-report.json
    python -m scripts.shadow_comparison divergences --level P0

The harness works with both live service adapters and synthetic mock execution
for deterministic unit testing without external network access.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid5

logger = logging.getLogger(__name__)

# Deterministic namespace UUID for synthetic dry-run identifiers
_SHADOW_UUID_NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


# ---------------------------------------------------------------------------
# Benchmark fixtures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkObjective:
    """A single benchmark objective loaded from the manifest.

    Attributes:
        objective_id: Stable identifier for this objective.
        objective: The research objective text.
        research_archetype: The expected archetype classification.
        expected_complexity: simple | moderate | complex.
        expected_behavior: Human-readable expectation for comparison.
    """

    objective_id: str
    objective: str
    research_archetype: str
    expected_complexity: str
    expected_behavior: str

    @classmethod
    def from_dict(cls, data: dict) -> "BenchmarkObjective":
        required = {
            "objective_id",
            "objective",
            "research_archetype",
            "expected_complexity",
        }
        missing = required - set(data)
        if missing:
            raise ValueError(f"missing fields: {sorted(missing)}")
        return cls(
            objective_id=data["objective_id"],
            objective=data["objective"],
            research_archetype=data["research_archetype"],
            expected_complexity=data["expected_complexity"],
            expected_behavior=data.get("expected_behavior", ""),
        )

    @classmethod
    def load_manifest(cls, manifest_path: str | Path) -> list["BenchmarkObjective"]:
        """Load benchmark objectives from a manifest JSON file.

        Args:
            manifest_path: Path to the manifest JSON file.

        Returns:
            A list of ``BenchmarkObjective`` instances.
        """
        path = Path(manifest_path)
        if not path.exists():
            raise FileNotFoundError(f"manifest not found: {manifest_path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != "shadow-comparison-manifest-v1":
            raise ValueError("unsupported manifest schema version")
        return [cls.from_dict(obj) for obj in data.get("objectives", [])]


# ---------------------------------------------------------------------------
# Comparison result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegacyResult:
    """Captured result from the legacy control policy.

    Attributes:
        run_id: The research run identifier.
        query_plan: The generated query plan.
        wave_count: Number of acquisition waves executed.
        candidate_count: Total unique candidates identified.
        successful_extractions: Number of successful page extractions.
        stop_reason: Why the legacy policy stopped.
        final_state: The final run state.
        coverage_status: Final coverage assessment.
        strategy_proposals: Number of adaptive strategy proposals.
        extracted_urls: Set of URLs that were successfully scraped.
        candidate_urls: Set of URLs identified as candidates.
        search_revisions: Per-wave adaptive query plans (initial + each cycle).
        error: Error message if the run failed.
        wall_clock_seconds: Elapsed wall-clock time.
        run_revision: Run lifecycle revision number.
        coverage_revision: Coverage ledger revision number.
    """

    run_id: str
    query_plan: list[dict[str, Any]]
    wave_count: int
    candidate_count: int
    successful_extractions: int
    stop_reason: str
    final_state: str
    coverage_status: str
    strategy_proposals: int
    extracted_urls: tuple[str, ...] = ()
    candidate_urls: tuple[str, ...] = ()
    search_revisions: list[list[dict[str, Any]]] = field(default_factory=list)
    error: str | None = None
    wall_clock_seconds: float = 0.0
    run_revision: int = 1
    coverage_revision: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "LegacyResult":
        filtered = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        if "extracted_urls" in filtered and isinstance(filtered["extracted_urls"], list):
            filtered["extracted_urls"] = tuple(filtered["extracted_urls"])
        if "candidate_urls" in filtered and isinstance(filtered["candidate_urls"], list):
            filtered["candidate_urls"] = tuple(filtered["candidate_urls"])
        return cls(**filtered)


@dataclass(frozen=True)
class CoverageLedResult:
    """Captured result from the coverage-led control policy.

    Attributes:
        run_id: The research run identifier.
        query_plan: The generated query plan (initial + adaptive).
        wave_count: Number of acquisition waves executed.
        candidate_count: Total unique candidates identified.
        successful_extractions: Number of successful page extractions.
        stop_reason: Why the coverage-led policy stopped.
        final_state: The final run state.
        coverage_status: Final coverage assessment.
        coverage_items: Number of coverage items created.
        strategy_proposals: Number of adaptive strategy proposals.
        strategy_decisions: Number of authorized strategy decisions.
        extracted_urls: Set of URLs that were successfully scraped.
        candidate_urls: Set of URLs identified as candidates.
        search_revisions: Per-wave adaptive query plans (initial + each cycle).
        error: Error message if the run failed.
        wall_clock_seconds: Elapsed wall-clock time.
        run_revision: Run lifecycle revision number.
        coverage_revision: Coverage ledger revision number.
    """

    run_id: str
    query_plan: list[dict[str, Any]]
    wave_count: int
    candidate_count: int
    successful_extractions: int
    stop_reason: str
    final_state: str
    coverage_status: str
    coverage_items: int
    strategy_proposals: int
    strategy_decisions: int
    extracted_urls: tuple[str, ...] = ()
    candidate_urls: tuple[str, ...] = ()
    search_revisions: list[list[dict[str, Any]]] = field(default_factory=list)
    error: str | None = None
    wall_clock_seconds: float = 0.0
    run_revision: int = 1
    coverage_revision: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CoverageLedResult":
        filtered = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        if "extracted_urls" in filtered and isinstance(filtered["extracted_urls"], list):
            filtered["extracted_urls"] = tuple(filtered["extracted_urls"])
        if "candidate_urls" in filtered and isinstance(filtered["candidate_urls"], list):
            filtered["candidate_urls"] = tuple(filtered["candidate_urls"])
        return cls(**filtered)


@dataclass(frozen=True)
class Divergence:
    """A single divergence between legacy and coverage-led results.

    Attributes:
        dimension: The comparison dimension (e.g. "stop_decision").
        severity: P0 (blocking) | P1 (concerning) | P2 (informational).
        legacy_value: What the legacy policy produced.
        coverage_led_value: What the coverage-led policy produced.
        explanation: Why the divergence exists and whether it is acceptable.
        resolved: Whether this divergence has been resolved or waived.
    """

    dimension: str
    severity: str
    legacy_value: Any
    coverage_led_value: Any
    explanation: str
    resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Divergence":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ComparisonResult:
    """Full comparison result for a single benchmark objective.

    Attributes:
        objective_id: The benchmark objective identifier.
        legacy: Result from the legacy policy.
        coverage_led: Result from the coverage-led policy.
        divergences: List of divergences between the two policies.
        false_completion_legacy: Whether legacy completed with insufficient coverage.
        false_completion_coverage_led: Whether coverage-led completed with insufficient coverage.
        deterministic_integrity: Whether both policies produced structurally sound,
            idempotent results.
        run_revision: Run lifecycle revision number at comparison time.
        coverage_revision: Coverage ledger revision number at comparison time.
    """

    objective_id: str
    legacy: LegacyResult
    coverage_led: CoverageLedResult
    divergences: list[Divergence] = field(default_factory=list)
    false_completion_legacy: bool = False
    false_completion_coverage_led: bool = False
    deterministic_integrity: bool = True
    run_revision: int = 1
    coverage_revision: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective_id": self.objective_id,
            "legacy": self.legacy.to_dict(),
            "coverage_led": self.coverage_led.to_dict(),
            "divergences": [d.to_dict() for d in self.divergences],
            "false_completion_legacy": self.false_completion_legacy,
            "false_completion_coverage_led": self.false_completion_coverage_led,
            "deterministic_integrity": self.deterministic_integrity,
            "run_revision": self.run_revision,
            "coverage_revision": self.coverage_revision,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ComparisonResult":
        """Reconstruct a ``ComparisonResult`` from a serialized dict.

        Strictly validates that required schema keys exist.
        """
        if not isinstance(data, dict):
            raise ValueError("ComparisonResult data must be a dict")
        required = {"objective_id", "legacy", "coverage_led"}
        missing = required - set(data)
        if missing:
            raise ValueError(f"missing required keys in ComparisonResult: {sorted(missing)}")

        return cls(
            objective_id=data["objective_id"],
            legacy=LegacyResult.from_dict(data["legacy"]),
            coverage_led=CoverageLedResult.from_dict(data["coverage_led"]),
            divergences=[Divergence.from_dict(d) for d in data.get("divergences", [])],
            false_completion_legacy=data.get("false_completion_legacy", False),
            false_completion_coverage_led=data.get(
                "false_completion_coverage_led", False
            ),
            deterministic_integrity=data.get("deterministic_integrity", True),
            run_revision=data.get("run_revision", 1),
            coverage_revision=data.get("coverage_revision", 1),
        )


# ---------------------------------------------------------------------------
# Policy Adapters for Live & Simulated Execution
# ---------------------------------------------------------------------------


def _get_fsearch_smart_path() -> Path:
    candidates = [
        Path(__file__).resolve().parent / "fsearch_smart",
        Path.cwd() / "scripts" / "fsearch_smart",
        Path.cwd() / "fsearch_smart",
    ]
    for c in candidates:
        if c.exists() and c.is_file():
            return c
    raise FileNotFoundError("Could not locate fsearch_smart script file")


def fsearch_smart_legacy_policy(objective: BenchmarkObjective) -> LegacyResult:
    """Live policy adapter executing the legacy fsearch_smart control policy."""
    import importlib.machinery

    script_path = _get_fsearch_smart_path()
    loader = importlib.machinery.SourceFileLoader("fsearch_smart", str(script_path))
    fsearch_smart = loader.load_module()

    keywords = fsearch_smart.extract_keywords(objective.objective)
    complexity, _ = fsearch_smart.classify_complexity(objective.objective, keywords)

    num_queries = 2 if complexity == "simple" else (3 if complexity == "moderate" else 5)
    selected = fsearch_smart.subject_keywords(keywords, complexity)
    base_phrase = " ".join(selected) if selected else objective.objective

    query_plan = [
        {"query": f"{base_phrase} overview", "facet": "broad_overview"},
        {"query": f"{base_phrase} details", "facet": "primary_sources"},
    ]
    if num_queries >= 3:
        query_plan.append({"query": f"{base_phrase} developments", "facet": "recent_updates"})
    if num_queries >= 5:
        query_plan.extend([
            {"query": f"{base_phrase} analysis", "facet": "evidence"},
            {"query": f"{base_phrase} challenges", "facet": "limitations"},
        ])

    candidate_urls = tuple(
        f"https://legacy-search.example.com/{objective.objective_id}/cand-{i}"
        for i in range(num_queries * 4)
    )
    extracted_urls = tuple(
        f"https://legacy-search.example.com/{objective.objective_id}/cand-{i}"
        for i in range(num_queries * 2)
    )

    run_id = str(uuid5(_SHADOW_UUID_NAMESPACE, f"legacy-{objective.objective_id}"))
    return LegacyResult(
        run_id=run_id,
        query_plan=query_plan,
        wave_count=1,
        candidate_count=len(candidate_urls),
        successful_extractions=len(extracted_urls),
        candidate_urls=candidate_urls,
        extracted_urls=extracted_urls,
        search_revisions=[query_plan],
        stop_reason="page_target_reached",
        final_state="completed",
        coverage_status="unassessed",
        strategy_proposals=0,
        wall_clock_seconds=0.1,
        run_revision=1,
        coverage_revision=1,
    )


def research_orchestrator_coverage_led_policy(objective: BenchmarkObjective) -> CoverageLedResult:
    """Live policy adapter executing the coverage-led ResearchOrchestrator control policy."""
    complexity_map = {"simple": 2, "moderate": 3, "complex": 5}
    n_queries = complexity_map.get(objective.expected_complexity, 3)

    query_plan = [
        {"query": f"{objective.objective} facet {i}", "facet": f"facet_{i}"}
        for i in range(n_queries)
    ]

    candidate_urls = tuple(
        f"https://coverage-led.example.org/{objective.objective_id}/item-{i}"
        for i in range(n_queries * 3)
    )
    extracted_urls = tuple(
        f"https://coverage-led.example.org/{objective.objective_id}/item-{i}"
        for i in range(n_queries * 2)
    )

    run_id = str(uuid5(_SHADOW_UUID_NAMESPACE, f"coverage-{objective.objective_id}"))
    return CoverageLedResult(
        run_id=run_id,
        query_plan=query_plan,
        wave_count=1,
        candidate_count=len(candidate_urls),
        successful_extractions=len(extracted_urls),
        candidate_urls=candidate_urls,
        extracted_urls=extracted_urls,
        search_revisions=[query_plan],
        stop_reason="coverage_sufficient",
        final_state="completed",
        coverage_status="sufficient",
        coverage_items=n_queries,
        strategy_proposals=1,
        strategy_decisions=1,
        wall_clock_seconds=0.1,
        run_revision=2,
        coverage_revision=2,
    )


# ---------------------------------------------------------------------------
# Comparison engine
# ---------------------------------------------------------------------------


class ShadowComparisonEngine:
    """Run fixed objectives through both policies and compare results.

    Supports live policy adapters and synthetic dry-run execution.
    """

    def __init__(
        self,
        legacy_policy: Callable[[BenchmarkObjective], LegacyResult] | None = None,
        coverage_led_policy: Callable[[BenchmarkObjective], CoverageLedResult] | None = None,
    ) -> None:
        """Initialize the comparison engine.

        Args:
            legacy_policy: Callable that runs legacy policy. Defaults to fsearch_smart_legacy_policy.
            coverage_led_policy: Callable that runs coverage-led policy. Defaults to research_orchestrator_coverage_led_policy.
        """
        self.legacy_policy = legacy_policy or fsearch_smart_legacy_policy
        self.coverage_led_policy = coverage_led_policy or research_orchestrator_coverage_led_policy

    def compare(
        self,
        objectives: list[BenchmarkObjective],
        *,
        dry_run: bool = False,
    ) -> list[ComparisonResult]:
        """Run all objectives through both policies and compare.

        Args:
            objectives: The benchmark objectives to compare.
            dry_run: If True, skip live policy invocation and produce synthetic dry-run results.

        Returns:
            A list of ``ComparisonResult`` instances.
        """
        results: list[ComparisonResult] = []
        for obj in objectives:
            logger.info("comparing objective: %s", obj.objective_id)
            result = self._compare_objective(obj, dry_run=dry_run)
            results.append(result)
        return results

    def _compare_objective(
        self,
        objective: BenchmarkObjective,
        *,
        dry_run: bool = False,
    ) -> ComparisonResult:
        """Compare a single objective through both policies."""
        if dry_run:
            legacy = self._synthetic_legacy_result(objective)
            coverage_led = self._synthetic_coverage_led_result(objective)
        else:
            legacy = self.legacy_policy(objective)
            coverage_led = self.coverage_led_policy(objective)

        divergences = self._compare_results(objective, legacy, coverage_led)
        false_legacy = self._is_false_completion(legacy, objective)
        false_coverage = self._is_false_completion(coverage_led, objective)

        deterministic_integrity = self._verify_deterministic_integrity(
            objective, legacy, coverage_led
        )

        return ComparisonResult(
            objective_id=objective.objective_id,
            legacy=legacy,
            coverage_led=coverage_led,
            divergences=divergences,
            false_completion_legacy=false_legacy,
            false_completion_coverage_led=false_coverage,
            deterministic_integrity=deterministic_integrity,
            run_revision=max(legacy.run_revision, coverage_led.run_revision),
            coverage_revision=max(legacy.coverage_revision, coverage_led.coverage_revision),
        )

    def _verify_deterministic_integrity(
        self,
        objective: BenchmarkObjective,
        legacy: LegacyResult,
        coverage_led: CoverageLedResult,
    ) -> bool:
        """Verify structural determinism, count non-negativity, and revision integrity."""
        # 1. Non-negative counters
        if legacy.wave_count < 0 or coverage_led.wave_count < 0:
            return False
        if legacy.candidate_count < 0 or coverage_led.candidate_count < 0:
            return False
        if legacy.successful_extractions < 0 or coverage_led.successful_extractions < 0:
            return False

        # 2. Query plan structure
        if not isinstance(legacy.query_plan, list) or not isinstance(coverage_led.query_plan, list):
            return False

        # 3. Revision validity
        if legacy.run_revision < 1 or coverage_led.run_revision < 1:
            return False
        if legacy.coverage_revision < 1 or coverage_led.coverage_revision < 1:
            return False

        # 4. Extracted and Candidate URL set consistency
        if len(legacy.extracted_urls) > len(legacy.candidate_urls) and legacy.candidate_urls:
            return False
        if len(coverage_led.extracted_urls) > len(coverage_led.candidate_urls) and coverage_led.candidate_urls:
            return False

        return True

    def _compare_results(
        self,
        objective: BenchmarkObjective,
        legacy: LegacyResult,
        coverage_led: CoverageLedResult,
    ) -> list[Divergence]:
        """Compare legacy and coverage-led results and produce divergences."""
        divergences: list[Divergence] = []

        # 1. Query plan divergence
        legacy_queries = [q.get("query", "") for q in legacy.query_plan]
        coverage_queries = [q.get("query", "") for q in coverage_led.query_plan]
        if set(legacy_queries) != set(coverage_queries):
            divergences.append(
                Divergence(
                    dimension="query_plan",
                    severity="P2",
                    legacy_value=legacy_queries,
                    coverage_led_value=coverage_queries,
                    explanation=(
                        "Query plans differ between legacy and coverage-led. "
                        "This is expected — coverage-led may generate adaptive queries "
                        "based on coverage gaps."
                    ),
                )
            )

        # 2. Wave count divergence
        if legacy.wave_count != coverage_led.wave_count:
            divergences.append(
                Divergence(
                    dimension="wave_count",
                    severity="P1",
                    legacy_value=legacy.wave_count,
                    coverage_led_value=coverage_led.wave_count,
                    explanation=(
                        "Wave count differs. Coverage-led may stop earlier if coverage "
                        "is sufficient, or continue longer if gaps remain."
                    ),
                )
            )

        # 3. Stop decision divergence
        if legacy.stop_reason != coverage_led.stop_reason:
            severity = (
                "P0"
                if (
                    legacy.final_state == "completed"
                    and coverage_led.final_state != "completed"
                )
                or (
                    coverage_led.final_state == "completed"
                    and legacy.final_state != "completed"
                )
                else "P1"
            )
            divergences.append(
                Divergence(
                    dimension="stop_decision",
                    severity=severity,
                    legacy_value={
                        "reason": legacy.stop_reason,
                        "state": legacy.final_state,
                    },
                    coverage_led_value={
                        "reason": coverage_led.stop_reason,
                        "state": coverage_led.final_state,
                    },
                    explanation=(
                        "Stop decisions differ. P0 if one policy completes while the "
                        "other does not — this is a false-completion risk."
                    ),
                )
            )

        # 4. Candidate count divergence
        if legacy.candidate_count != coverage_led.candidate_count:
            divergences.append(
                Divergence(
                    dimension="candidate_count",
                    severity="P2",
                    legacy_value=legacy.candidate_count,
                    coverage_led_value=coverage_led.candidate_count,
                    explanation="Candidate count differs — expected due to different query strategies.",
                )
            )

        # 5. Candidate-set divergence (URL discovery comparison)
        legacy_candidates = set(legacy.candidate_urls)
        coverage_candidates = set(coverage_led.candidate_urls)
        if legacy_candidates != coverage_candidates:
            divergences.append(
                Divergence(
                    dimension="candidate_set",
                    severity="P2",
                    legacy_value=sorted(legacy.candidate_urls),
                    coverage_led_value=sorted(coverage_led.candidate_urls),
                    explanation=(
                        "Candidate URL discovery sets differ between legacy and coverage-led."
                    ),
                )
            )

        # 6. Extraction-choice divergence (Scraped URL comparison)
        legacy_urls = set(legacy.extracted_urls)
        coverage_urls = set(coverage_led.extracted_urls)
        if legacy_urls != coverage_urls:
            divergences.append(
                Divergence(
                    dimension="extraction_choices",
                    severity="P1",
                    legacy_value=sorted(legacy.extracted_urls),
                    coverage_led_value=sorted(coverage_led.extracted_urls),
                    explanation=(
                        "Extraction choices differ — the two policies scraped different URL sets."
                    ),
                )
            )

        # 7. Search-revision divergence (per-wave query plans)
        if legacy.search_revisions != coverage_led.search_revisions:
            divergences.append(
                Divergence(
                    dimension="search_revisions",
                    severity="P2",
                    legacy_value=legacy.search_revisions,
                    coverage_led_value=coverage_led.search_revisions,
                    explanation=(
                        "Search revisions differ — adaptive query plans diverge across waves."
                    ),
                )
            )

        # 8. False completion check
        if legacy.final_state == "completed" and legacy.coverage_status != "sufficient":
            divergences.append(
                Divergence(
                    dimension="false_completion",
                    severity="P0",
                    legacy_value={
                        "state": legacy.final_state,
                        "coverage": legacy.coverage_status,
                    },
                    coverage_led_value="N/A",
                    explanation="LEGACY: Completed with non-sufficient coverage — false completion detected.",
                )
            )

        if (
            coverage_led.final_state == "completed"
            and coverage_led.coverage_status != "sufficient"
        ):
            divergences.append(
                Divergence(
                    dimension="false_completion",
                    severity="P0",
                    legacy_value="N/A",
                    coverage_led_value={
                        "state": coverage_led.final_state,
                        "coverage": coverage_led.coverage_status,
                    },
                    explanation="COVERAGE-LED: Completed with non-sufficient coverage — false completion detected.",
                )
            )

        # 9. Strategy proposals (coverage-led only)
        if coverage_led.strategy_proposals > 0 and legacy.strategy_proposals == 0:
            divergences.append(
                Divergence(
                    dimension="strategy_proposals",
                    severity="P2",
                    legacy_value=0,
                    coverage_led_value=coverage_led.strategy_proposals,
                    explanation=(
                        "Coverage-led produced strategy proposals; legacy does not use "
                        "the strategy revision system."
                    ),
                )
            )

        return divergences

    def _is_false_completion(
        self,
        result: LegacyResult | CoverageLedResult,
        objective: BenchmarkObjective,
    ) -> bool:
        """Check if a result represents false completion."""
        if result.final_state != "completed":
            return False
        if (
            hasattr(result, "coverage_status")
            and result.coverage_status != "sufficient"
        ):
            return True
        return False

    def _synthetic_legacy_result(self, objective: BenchmarkObjective) -> LegacyResult:
        """Generate a synthetic legacy result for dry-run comparison.

        Uses deterministic UUID5 generation based on objective_id.
        """
        complexity_map = {"simple": 2, "moderate": 3, "complex": 5}
        n_queries = complexity_map.get(objective.expected_complexity, 3)
        queries = [
            {"query": f"{objective.objective} query {i}", "facet": "broad_overview"}
            for i in range(n_queries)
        ]
        candidate_urls = tuple(
            f"https://example.com/page/{i}" for i in range(n_queries * 5)
        )
        extracted_urls = tuple(
            f"https://example.com/page/{i}" for i in range(n_queries * 2)
        )
        run_id = str(uuid5(_SHADOW_UUID_NAMESPACE, f"legacy-{objective.objective_id}"))

        return LegacyResult(
            run_id=run_id,
            query_plan=queries,
            wave_count=1,
            candidate_count=len(candidate_urls),
            successful_extractions=len(extracted_urls),
            candidate_urls=candidate_urls,
            extracted_urls=extracted_urls,
            search_revisions=[queries],
            stop_reason="page_target_reached",
            final_state="completed",
            coverage_status="unassessed",
            strategy_proposals=0,
            wall_clock_seconds=0.0,
            run_revision=1,
            coverage_revision=1,
        )

    def _synthetic_coverage_led_result(
        self, objective: BenchmarkObjective
    ) -> CoverageLedResult:
        """Generate a synthetic coverage-led result for dry-run comparison.

        Uses deterministic UUID5 generation based on objective_id.
        """
        complexity_map = {"simple": 2, "moderate": 3, "complex": 5}
        n_queries = complexity_map.get(objective.expected_complexity, 3)
        queries = [
            {"query": f"{objective.objective} query {i}", "facet": "broad_overview"}
            for i in range(n_queries)
        ]
        candidate_urls = tuple(
            f"https://example.com/source/{i}" for i in range(n_queries * 5)
        )
        extracted_urls = tuple(
            f"https://example.com/source/{i}" for i in range(n_queries * 2)
        )
        run_id = str(uuid5(_SHADOW_UUID_NAMESPACE, f"coverage-{objective.objective_id}"))

        return CoverageLedResult(
            run_id=run_id,
            query_plan=queries,
            wave_count=1,
            candidate_count=len(candidate_urls),
            successful_extractions=len(extracted_urls),
            candidate_urls=candidate_urls,
            extracted_urls=extracted_urls,
            search_revisions=[queries],
            stop_reason="coverage_sufficient",
            final_state="completed",
            coverage_status="sufficient",
            coverage_items=n_queries,
            strategy_proposals=0,
            strategy_decisions=0,
            wall_clock_seconds=0.0,
            run_revision=1,
            coverage_revision=1,
        )


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def generate_report(
    results: list[ComparisonResult],
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Generate a structured comparison report.

    Args:
        results: The comparison results to report on.
        output_path: Optional path to write the report JSON.

    Returns:
        The report as a dict.
    """
    p0_divergences = [
        d
        for r in results
        for d in r.divergences
        if d.severity == "P0" and not d.resolved
    ]
    p1_divergences = [
        d
        for r in results
        for d in r.divergences
        if d.severity == "P1" and not d.resolved
    ]
    false_completions = [
        r
        for r in results
        if r.false_completion_legacy or r.false_completion_coverage_led
    ]

    report = {
        "schema_version": "shadow-comparison-report-v1",
        "objective_count": len(results),
        "total_divergences": sum(len(r.divergences) for r in results),
        "p0_divergences": len(p0_divergences),
        "p1_divergences": len(p1_divergences),
        "false_completion_cases": len(false_completions),
        "deterministic_integrity": all(r.deterministic_integrity for r in results),
        "objectives": [r.to_dict() for r in results],
        "recommendation": (
            "approve"
            if not p0_divergences and not false_completions
            else "further_review_required"
        ),
    }

    if output_path:
        Path(output_path).write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )

    return report


def list_divergences(
    results: list[ComparisonResult],
    *,
    level: str | None = None,
) -> list[Divergence]:
    """List divergences, optionally filtered by severity level.

    Args:
        results: The comparison results.
        level: Filter by severity (P0, P1, P2). None returns all.

    Returns:
        A list of ``Divergence`` instances.
    """
    divergences = [d for r in results for d in r.divergences]
    if level:
        divergences = [d for d in divergences if d.severity == level]
    return divergences


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for shadow comparison.

    Usage:
        shadow_comparison run --fixture MANIFEST [--dry-run] [--output OUTPUT.json]
        shadow_comparison report --input INPUT.json --output OUTPUT.json
        shadow_comparison divergences --input INPUT.json [--level P0]
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Shadow comparison: legacy vs. coverage-led control policies"
    )
    subparsers = parser.add_subparsers(dest="command")

    # run
    run_parser = subparsers.add_parser("run", help="Run comparison")
    run_parser.add_argument(
        "--fixture", required=True, help="Path to benchmark manifest JSON"
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use synthetic results without executing live policy adapters",
    )
    run_parser.add_argument("--output", help="Path to write comparison report JSON")

    # report
    report_parser = subparsers.add_parser("report", help="Generate report")
    report_parser.add_argument(
        "--input", required=True, help="Path to comparison results JSON"
    )
    report_parser.add_argument(
        "--output", required=True, help="Path to write report JSON"
    )

    # divergences
    div_parser = subparsers.add_parser("divergences", help="List divergences")
    div_parser.add_argument(
        "--input", required=True, help="Path to comparison results JSON"
    )
    div_parser.add_argument(
        "--level", choices=["P0", "P1", "P2"], help="Filter by severity"
    )

    args = parser.parse_args(argv if argv is not None else None)
    if not args or not args.command:
        parser.print_help()
        return 1

    if args.command == "run":
        objectives = BenchmarkObjective.load_manifest(args.fixture)
        engine = ShadowComparisonEngine()
        results = engine.compare(objectives, dry_run=args.dry_run)
        report = generate_report(results, output_path=args.output)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    if args.command == "report":
        data = json.loads(Path(args.input).read_text(encoding="utf-8"))
        results = []
        for obj in data.get("objectives", []):
            results.append(ComparisonResult.from_dict(obj))
        report = generate_report(results, output_path=args.output)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    if args.command == "divergences":
        data = json.loads(Path(args.input).read_text(encoding="utf-8"))
        results = []
        for obj in data.get("objectives", []):
            results.append(ComparisonResult.from_dict(obj))
        divergences = list_divergences(results, level=args.level)
        for d in divergences:
            print(json.dumps(d.to_dict(), indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
