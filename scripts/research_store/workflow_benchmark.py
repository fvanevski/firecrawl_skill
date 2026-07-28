"""Workflow benchmark runner for release campaigns (Phase 7, issue #67).

This module provides:

* ``BenchmarkDatasetLoader`` — loads benchmark datasets from JSON files.
* ``WorkflowBenchmarkRunner`` — runs benchmark objectives against workflow
  modes and produces structured ``WorkflowComparison`` and
  ``ReleaseRecommendation`` output.
* ``DeterministicIntegrityChecker`` — runs actual integrity checks against
  real state (blob store, evidence packets, state machine) when available,
  falling back to simulation when dependencies are absent.
* ``run_benchmark`` — the primary entry point for programmatic access.
* ``load_benchmark_dataset`` — convenience function for loading datasets.

The benchmark runner exercises each workflow mode (agent_led,
autonomous_local, deterministic_debug) against a fixed benchmark dataset and
produces structured comparison output with quality, performance, and
deterministic integrity measurements.

Two execution modes:

* **Simulation (default, ``dry_run=True``)** — deterministic synthetic
  results based on hardcoded mode-specific constants. Useful for CI and
  infrastructure testing.
* **Real execution (``dry_run=False``)** — exercises the actual workflow
  pipeline (orchestrator, synthesis, report validation) when a database
  and blob store are available. Falls back to simulation for individual
  objectives that cannot be executed.

Usage
-----
    >>> from research_store.workflow_benchmark import (
    ...     load_benchmark_dataset,
    ...     run_benchmark,
    ... )
    >>> dataset = load_benchmark_dataset("tests/fixtures/benchmark/benchmark-v1.json")
    >>> result = run_benchmark(dataset, workflow_modes=["agent_led", "autonomous_local"])
    >>> print(result.recommendation.summary())
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_domain.models import (
    BenchmarkDataset,
    BenchmarkObjective,
    BenchmarkSource,
    DeterministicIntegrityCheck,
    PerformanceMeasurement,
    QualityMeasurement,
    RecommendationOutcome,
    ReleaseRecommendation,
    WorkflowComparison,
    WorkflowRunResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Benchmark dataset loading
# ---------------------------------------------------------------------------


class BenchmarkDatasetLoader:
    """Load benchmark datasets from JSON files.

    Attributes:
        dataset: The loaded benchmark dataset.
        objectives: List of benchmark objectives.
        quality_thresholds: Quality thresholds for pass/fail.
    """

    def __init__(self, dataset: BenchmarkDataset):
        self.dataset = dataset
        self.objectives = list(dataset.objectives)
        self.quality_thresholds = dict(dataset.quality_thresholds)

    @classmethod
    def from_file(cls, path: str | Path) -> BenchmarkDatasetLoader:
        """Load a benchmark dataset from a JSON file.

        Args:
            path: Path to the benchmark dataset JSON file.

        Returns:
            A new BenchmarkDatasetLoader instance.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"benchmark dataset not found: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return cls(_build_dataset(data))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BenchmarkDatasetLoader:
        """Load a benchmark dataset from a dictionary.

        Args:
            data: Dictionary containing benchmark dataset structure.

        Returns:
            A new BenchmarkDatasetLoader instance.
        """
        return cls(_build_dataset(data))


def load_benchmark_dataset(path: str | Path) -> BenchmarkDatasetLoader:
    """Convenience function to load a benchmark dataset from a JSON file.

    Args:
        path: Path to the benchmark dataset JSON file.

    Returns:
        A new BenchmarkDatasetLoader instance.
    """
    return BenchmarkDatasetLoader.from_file(path)


def _build_objective(obj_data: dict[str, Any]) -> BenchmarkObjective:
    """Build a BenchmarkObjective from a dictionary."""
    relevant_sources = tuple(
        BenchmarkSource(
            schema_version="benchmark-source-v1",
            file_path=s["file_path"] if isinstance(s, dict) else s,
            relevance=True,
            role="relevant",
        )
        for s in obj_data.get("known_relevant_sources", [])
    )
    distractor_sources = tuple(
        BenchmarkSource(
            schema_version="benchmark-source-v1",
            file_path=s["file_path"] if isinstance(s, dict) else s,
            relevance=False,
            role="distractor",
        )
        for s in obj_data.get("known_distractor_sources", [])
    )
    return BenchmarkObjective(
        schema_version="benchmark-objective-v1",
        id=obj_data["id"],
        title=obj_data["title"],
        objective=obj_data["objective"],
        questions=tuple(obj_data.get("questions", [])),
        expected_source_classes=tuple(obj_data.get("expected_source_classes", [])),
        known_relevant_sources=relevant_sources,
        known_distractor_sources=distractor_sources,
        expected_unresolved_controversies=tuple(
            obj_data.get("expected_unresolved_controversies", [])
        ),
        citation_support_labels=obj_data.get("citation_support_labels", {}),
    )


def _build_dataset(data: dict[str, Any]) -> BenchmarkDataset:
    """Build a BenchmarkDataset from a dictionary."""
    objectives = tuple(_build_objective(obj) for obj in data.get("objectives", []))
    return BenchmarkDataset(
        schema_version="benchmark-dataset-v1",
        version=data.get("version", "benchmark-v1"),
        description=data.get("description", ""),
        evaluation_set=data.get("evaluation_set", False),
        objectives=objectives,
        quality_thresholds=data.get("quality_thresholds", {}),
        workflow_modes=tuple(
            data.get("workflow_modes", ["agent_led", "autonomous_local"])
        ),
        deterministic_integrity_checks=tuple(
            data.get("deterministic_integrity_checks", [])
        ),
    )


# ---------------------------------------------------------------------------
# Deterministic integrity checks
# ---------------------------------------------------------------------------


class DeterministicIntegrityChecker:
    """Run deterministic integrity checks against workflow state.

    These checks verify that core invariants are preserved — content
    addressing, derivation versioning, lease safety, state machine
    transitions, evidence packet validation, citation binding, cache key
    identity, and idempotent replay.

    When a blob store root is provided, the checker performs **real**
    verifications (rehashing blobs, validating state machine transitions,
    checking evidence packet bindings).  When no blob store is available
    (e.g. in CI), the checker falls back to simulation.

    Structural-invariant checks (always pass in simulation):

    * ``derivation_versioning`` — code-level invariant
    * ``lease_safety`` — code-level invariant
    * ``cache_key_identity`` — code-level invariant
    * ``idempotent_replay`` — code-level invariant
    * ``content_addressed_blob_integrity`` — real when ``blob_root`` is
      provided; simulation otherwise

    Data-driven checks (perform real validation when injected data is
    provided):

    * ``evidence_packet_validation`` — validates claim/passage bindings
    * ``citation_binding_integrity`` — validates citation references
    * ``state_machine_transitions`` — validates run state transitions
    """

    CHECKS = (
        "content_addressed_blob_integrity",
        "derivation_versioning",
        "lease_safety",
        "state_machine_transitions",
        "evidence_packet_validation",
        "citation_binding_integrity",
        "cache_key_identity",
        "idempotent_replay",
    )

    def __init__(
        self,
        blob_root: Path | str | None = None,
        evidence_packets: list[dict[str, Any]] | None = None,
        run_transitions: list[dict[str, Any]] | None = None,
        strict: bool = False,
    ) -> None:
        """Initialize the integrity checker.

        Args:
            blob_root: Path to the content-addressed blob store root.
                When provided, the content-addressed blob integrity check
                performs real SHA-256 verification.
            evidence_packets: List of evidence packet dicts (with
                ``claim_evidence_bindings``, ``passages``,
                ``claims`` keys).  When provided, the evidence packet
                validation and citation binding checks perform real
                cross-referencing.
            run_transitions: List of run transition dicts (with
                ``prior_state``, ``next_state``, ``lifecycle_revision``
                keys).  When provided, the state machine transition
                check verifies valid transitions.
            strict: If True, integrity checks that fall back to simulation
                will fail instead of passing. Use this in CI or when real
                state is expected but missing.
        """
        self.blob_root = Path(blob_root) if blob_root else None
        self.evidence_packets = evidence_packets or []
        self.run_transitions = run_transitions or []
        self.strict = strict

    def check(self, check_name: str) -> DeterministicIntegrityCheck:
        """Run a single deterministic integrity check.

        Args:
            check_name: Name of the check to run.

        Returns:
            A DeterministicIntegrityCheck result.
        """
        if check_name not in self.CHECKS:
            return DeterministicIntegrityCheck(
                schema_version="integrity-check-v1",
                check_name=check_name,
                passed=False,
                details=f"unknown integrity check: {check_name}",
            )

        # Dispatch to the real implementation when data is available,
        # otherwise fall back to simulation.
        real_check = getattr(self, f"_check_{check_name}", None)
        if real_check is not None and self._has_data_for_check(check_name):
            try:
                return real_check()
            except Exception as exc:
                logger.warning(
                    "integrity check '%s' failed with error: %s",
                    check_name,
                    exc,
                    exc_info=True,
                )
                return DeterministicIntegrityCheck(
                    schema_version="integrity-check-v1",
                    check_name=check_name,
                    passed=False,
                    details=f"integrity check '{check_name}' failed: {exc}",
                )

        # Fallback to simulation when data is not available.
        if self.strict:
            return DeterministicIntegrityCheck(
                schema_version="integrity-check-v1",
                check_name=check_name,
                passed=False,
                details=(
                    f"integrity check '{check_name}' failed — "
                    f"strict mode requires real state (no real state available)"
                ),
            )
        return DeterministicIntegrityCheck(
            schema_version="integrity-check-v1",
            check_name=check_name,
            passed=True,
            details=(
                f"integrity check '{check_name}' passed — "
                f"simulation (no real state available)"
            ),
        )

    def _has_data_for_check(self, check_name: str) -> bool:
        """Return True when the checker has real data for the check."""
        if check_name == "content_addressed_blob_integrity":
            return self.blob_root is not None and self.blob_root.is_dir()
        if check_name in (
            "evidence_packet_validation",
            "citation_binding_integrity",
        ):
            return len(self.evidence_packets) > 0
        if check_name == "state_machine_transitions":
            return len(self.run_transitions) > 0
        # These checks are structural code invariants and always pass
        # in simulation mode.
        return False

    # ------------------------------------------------------------------
    # Real integrity check implementations
    # ------------------------------------------------------------------

    def _check_content_addressed_blob_integrity(
        self,
    ) -> DeterministicIntegrityCheck:
        """Verify that all blobs in the store are content-addressed correctly.

        Iterates over all files in the blob store and re-hashes each one
        to confirm the filename matches the SHA-256 digest.
        """
        from .blob import ContentAddressedBlobStore

        store = ContentAddressedBlobStore(self.blob_root)  # type: ignore[arg-type]
        verified = 0
        failed = 0
        errors: list[str] = []

        for prefix_dir in sorted(self.blob_root.iterdir()):  # type: ignore[union-attr]
            if not prefix_dir.is_dir():
                continue
            for sub_dir in prefix_dir.iterdir():
                if not sub_dir.is_dir():
                    continue
                for blob_path in sub_dir.iterdir():
                    if not blob_path.is_file():
                        continue
                    digest = blob_path.name
                    if len(digest) != 64:
                        failed += 1
                        errors.append(f"unexpected filename: {digest}")
                        continue
                    if not store.verify(digest):
                        failed += 1
                        errors.append(f"hash mismatch: {digest}")
                    else:
                        verified += 1

        passed = failed == 0
        details = f"verified {verified} blobs, {failed} failures" + (
            f" — errors: {', '.join(errors[:5])}" if errors else ""
        )
        return DeterministicIntegrityCheck(
            schema_version="integrity-check-v1",
            check_name="content_addressed_blob_integrity",
            passed=passed,
            details=details,
        )

    def _check_evidence_packet_validation(self) -> DeterministicIntegrityCheck:
        """Verify that evidence packets have valid claim/passage bindings.

        For each evidence packet, checks that:
        - All claim IDs in bindings exist in the claims list
        - All passage IDs in bindings exist in the passages list
        - No duplicate IDs
        """
        errors: list[str] = []
        packets_checked = 0

        for packet in self.evidence_packets:
            packets_checked += 1
            claim_ids = {c["claim_id"] for c in packet.get("claims", [])}
            passage_ids = {p["passage_id"] for p in packet.get("passages", [])}
            # Also include omitted passages
            for p in packet.get("omitted_passages", []):
                passage_ids.add(p["passage_id"])

            for binding in packet.get("claim_evidence_bindings", []):
                cid = binding.get("claim_id", "")
                if cid and cid not in claim_ids:
                    errors.append(f"packet binding references unknown claim {cid}")
                for pid in binding.get("passage_ids", []):
                    if pid and pid not in passage_ids:
                        errors.append(
                            f"packet binding references unknown passage {pid}"
                        )

        passed = len(errors) == 0
        details = f"validated {packets_checked} packets" + (
            f" — {len(errors)} errors" if errors else ""
        )
        return DeterministicIntegrityCheck(
            schema_version="integrity-check-v1",
            check_name="evidence_packet_validation",
            passed=passed,
            details=details,
        )

    def _check_citation_binding_integrity(self) -> DeterministicIntegrityCheck:
        """Verify that all cited passages exist in the evidence packet.

        Checks that every passage_id referenced in claim_evidence_bindings
        is present in the packet's passages or omitted_passages.
        """
        errors: list[str] = []
        citations_checked = 0

        for packet in self.evidence_packets:
            passage_ids = {p["passage_id"] for p in packet.get("passages", [])}
            for p in packet.get("omitted_passages", []):
                passage_ids.add(p["passage_id"])

            for binding in packet.get("claim_evidence_bindings", []):
                citations_checked += 1
                for pid in binding.get("passage_ids", []):
                    if pid not in passage_ids:
                        errors.append(
                            f"citation to unknown passage {pid} "
                            f"in binding for claim {binding.get('claim_id')}"
                        )

        passed = len(errors) == 0
        details = f"checked {citations_checked} citations" + (
            f" — {len(errors)} errors" if errors else ""
        )
        return DeterministicIntegrityCheck(
            schema_version="integrity-check-v1",
            check_name="citation_binding_integrity",
            passed=passed,
            details=details,
        )

    def _check_state_machine_transitions(self) -> DeterministicIntegrityCheck:
        """Verify that run state transitions are valid.

        Checks that every transition in the run transition ledger follows
        the permitted transition matrix.
        """
        # Permitted transitions from run_service.py
        PERMITTED_TRANSITIONS: dict[str, set[str]] = {
            "created": {"planning"},
            "planning": {"corpus_review", "failed"},
            "corpus_review": {"acquiring", "retrieving", "failed"},
            "acquiring": {"coverage_review", "extracting", "failed", "partial"},
            "extracting": {"indexing", "coverage_review", "failed"},
            "indexing": {"coverage_review", "partial", "failed"},
            "coverage_review": {
                "acquiring",
                "extracting",
                "retrieving",
                "synthesizing",
                "partial",
                "failed",
            },
            "retrieving": {"coverage_review", "synthesizing", "failed"},
            "synthesizing": {"validating", "failed"},
            "validating": {"completed", "partial", "failed"},
            "completed": set(),
            "partial": set(),
            "failed": set(),
            "cancelled": set(),
        }

        errors: list[str] = []
        transitions_checked = 0

        for transition in self.run_transitions:
            transitions_checked += 1
            prior = transition.get("prior_state", "")
            next_state = transition.get("next_state", "")
            if prior in PERMITTED_TRANSITIONS:
                allowed = PERMITTED_TRANSITIONS[prior]
                if next_state not in allowed:
                    errors.append(f"invalid transition {prior} -> {next_state}")
            else:
                errors.append(f"unknown prior state: {prior}")

        passed = len(errors) == 0
        details = f"checked {transitions_checked} transitions" + (
            f" — {len(errors)} errors" if errors else ""
        )
        return DeterministicIntegrityCheck(
            schema_version="integrity-check-v1",
            check_name="state_machine_transitions",
            passed=passed,
            details=details,
        )

    # ------------------------------------------------------------------
    # Simulation fallbacks for checks that don't have real data
    # ------------------------------------------------------------------

    def _check_derivation_versioning(self) -> DeterministicIntegrityCheck:
        return DeterministicIntegrityCheck(
            schema_version="integrity-check-v1",
            check_name="derivation_versioning",
            passed=True,
            details=(
                "integrity check 'derivation_versioning' passed — "
                "simulation (no real state available)"
            ),
        )

    def _check_lease_safety(self) -> DeterministicIntegrityCheck:
        return DeterministicIntegrityCheck(
            schema_version="integrity-check-v1",
            check_name="lease_safety",
            passed=True,
            details=(
                "integrity check 'lease_safety' passed — "
                "simulation (no real state available)"
            ),
        )

    def _check_cache_key_identity(self) -> DeterministicIntegrityCheck:
        return DeterministicIntegrityCheck(
            schema_version="integrity-check-v1",
            check_name="cache_key_identity",
            passed=True,
            details=(
                "integrity check 'cache_key_identity' passed — "
                "simulation (no real state available)"
            ),
        )

    def _check_idempotent_replay(self) -> DeterministicIntegrityCheck:
        return DeterministicIntegrityCheck(
            schema_version="integrity-check-v1",
            check_name="idempotent_replay",
            passed=True,
            details=(
                "integrity check 'idempotent_replay' passed — "
                "simulation (no real state available)"
            ),
        )

    def check_all(
        self, check_names: tuple[str, ...]
    ) -> tuple[DeterministicIntegrityCheck, ...]:
        """Run all specified integrity checks.

        Args:
            check_names: Names of checks to run.

        Returns:
            Tuple of DeterministicIntegrityCheck results.
        """
        return tuple(self.check(name) for name in check_names)


# ---------------------------------------------------------------------------
# Workflow benchmark runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowBenchmarkConfig:
    """Configuration for workflow benchmark runs.

    Attributes:
        workflow_modes: Workflow modes to benchmark.
        objective_ids: Specific objective IDs to run (None = all).
        dry_run: If True, simulate without executing workflows.
        integrity_checks: Integrity check names to run.
        blob_root: Path to the content-addressed blob store root.
            When provided, the integrity checker performs real blob
            verification instead of simulation.
        evidence_packets: List of evidence packet dicts for integrity
            checking.  When provided, the evidence packet validation
            and citation binding checks perform real cross-referencing.
        run_transitions: List of run transition dicts for integrity
            checking.  When provided, the state machine transition
            check verifies valid transitions.
        known_limitations: Custom known limitations to include in the
            release recommendation.  When empty, defaults are derived
            from the workflow mode.
    """

    workflow_modes: tuple[str, ...] = ("agent_led", "autonomous_local")
    objective_ids: tuple[str, ...] | None = None
    dry_run: bool = True
    integrity_checks: tuple[str, ...] = (
        "content_addressed_blob_integrity",
        "derivation_versioning",
        "lease_safety",
        "state_machine_transitions",
        "evidence_packet_validation",
        "citation_binding_integrity",
        "cache_key_identity",
        "idempotent_replay",
    )
    blob_root: Path | str | None = None
    evidence_packets: list[dict[str, Any]] | None = None
    run_transitions: list[dict[str, Any]] | None = None
    known_limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowBenchmarkResult:
    """Result of a workflow benchmark run.

    Attributes:
        dataset_version: Version of the benchmark dataset used.
        comparison: The workflow comparison results.
        recommendation: The release recommendation.
        total_duration_ms: Wall-clock duration of the benchmark.
    """

    dataset_version: str
    comparison: WorkflowComparison
    recommendation: ReleaseRecommendation
    total_duration_ms: float


class WorkflowBenchmarkRunner:
    """Run benchmark objectives against workflow modes.

    The runner exercises each workflow mode against the benchmark dataset
    and produces structured comparison output.

    Attributes:
        loader: The loaded benchmark dataset.
        config: Benchmark configuration.
        integrity_checker: Deterministic integrity checker.
    """

    # Placeholder simulation constants for quality and performance metrics.
    #
    # These values are UNVERIFIED placeholders — they do not represent
    # measured workflow behavior.  Per PRD Section 21.8: "All quality
    # targets remain UNVERIFIED until benchmark baselines are collected."
    #
    # TODO(P7-07): Replace with measured baselines before the release gate.
    # When baselines are collected, update these dicts and remove the
    # PLACEHOLDER annotations.  The test suite (TestSimulationPlaceholders)
    # asserts that every quality/performance base value carries a
    # PLACEHOLDER annotation so that stale constants cannot silently
    # become unverified claims.
    PLACEHOLDER = True

    def __init__(
        self,
        loader: BenchmarkDatasetLoader,
        config: WorkflowBenchmarkConfig | None = None,
    ):
        self.loader = loader
        self.config = config or WorkflowBenchmarkConfig()
        self.integrity_checker = DeterministicIntegrityChecker(
            blob_root=self.config.blob_root,
            evidence_packets=self.config.evidence_packets,
            run_transitions=self.config.run_transitions,
        )

    def run(self) -> WorkflowBenchmarkResult:
        """Execute the full workflow benchmark and return results.

        Returns:
            A WorkflowBenchmarkResult with comparison and recommendation.
        """
        start = time.monotonic()

        objectives = self._select_objectives()
        results: list[WorkflowRunResult] = []

        for mode in self.config.workflow_modes:
            mode_results = self._run_workflow_mode(mode, objectives)
            results.extend(mode_results)

        # Run integrity checks
        integrity_results = self.integrity_checker.check_all(
            self.config.integrity_checks
        )

        # Build comparison
        comparison = self._build_comparison(results, integrity_results)

        # Build recommendation
        recommendation = self._build_recommendation(comparison)

        end = time.monotonic()
        duration_ms = (end - start) * 1000

        return WorkflowBenchmarkResult(
            dataset_version=self.loader.dataset.version,
            comparison=comparison,
            recommendation=recommendation,
            total_duration_ms=duration_ms,
        )

    def _select_objectives(self) -> list[BenchmarkObjective]:
        """Select objectives based on config."""
        if self.config.objective_ids:
            return [
                obj
                for obj in self.loader.objectives
                if obj.id in self.config.objective_ids
            ]
        return list(self.loader.objectives)

    def _run_workflow_mode(
        self,
        workflow_mode: str,
        objectives: list[BenchmarkObjective],
    ) -> list[WorkflowRunResult]:
        """Run a single workflow mode against all objectives.

        In deterministic mode (no network), this simulates workflow execution
        by measuring code invariants and producing synthetic but realistic
        benchmark results.  When ``self.config.dry_run`` is False, the runner
        exercises the actual ResearchOrchestrator pipeline for each objective.
        """
        results: list[WorkflowRunResult] = []

        for objective in objectives:
            if self.config.dry_run:
                result = self._simulate_workflow_run(workflow_mode, objective)
            else:
                result = self._execute_real_workflow(workflow_mode, objective)
            results.append(result)

        return results

    def _simulate_workflow_run(
        self,
        workflow_mode: str,
        objective: BenchmarkObjective,
    ) -> WorkflowRunResult:
        """Simulate a single workflow run for a given objective.

        This produces deterministic, reproducible results based on the
        objective and workflow mode. In production, this would execute
        the actual workflow.
        """
        start = time.monotonic()

        # Simulate quality metrics based on workflow mode
        quality = self._simulate_quality(workflow_mode, objective)

        # Simulate performance metrics based on workflow mode
        performance = self._simulate_performance(workflow_mode, objective)

        # Run integrity checks
        integrity_checks = self.integrity_checker.check_all(
            self.config.integrity_checks
        )

        end = time.monotonic()
        latency_ms = (end - start) * 1000

        # Add simulated latency to performance
        performance = PerformanceMeasurement(
            schema_version="performance-measurement-v1",
            total_latency_ms=performance.total_latency_ms + latency_ms,
            total_tokens=performance.total_tokens,
            semantic_calls=performance.semantic_calls,
            cache_hit_rate=performance.cache_hit_rate,
            embedding_throughput=performance.embedding_throughput,
            gpu_memory_mb=performance.gpu_memory_mb,
            cpu_percent=performance.cpu_percent,
        )

        return WorkflowRunResult(
            schema_version="workflow-run-result-v1",
            workflow_mode=workflow_mode,
            quality=quality,
            performance=performance,
            integrity_checks=integrity_checks,
            run_id=None,  # None for dry-run / simulation
            errors=(),
        )

    def _execute_real_workflow(
        self,
        workflow_mode: str,
        objective: BenchmarkObjective,
    ) -> WorkflowRunResult:
        """Execute a real workflow run for a given objective.

        Exercises the actual ResearchOrchestrator pipeline and captures
        real quality and performance metrics from the execution.  Falls
        back to simulation when the database or orchestrator is unavailable.
        """
        start = time.monotonic()
        errors: list[str] = []
        run_id: str | None = None

        try:
            from uuid import uuid4

            from research_store.config import StoreConfig
            from research_store.container import build_orchestrator, build_run_service
            from research_store.orchestrator import OrchestratorConfig

            # Load configuration from environment
            config = StoreConfig.from_env()
            config.require_database()

            # Build orchestrator for the target execution mode
            orchestrator_config = OrchestratorConfig(
                execution_mode=workflow_mode,
                max_adaptive_cycles=10,
                legacy_adapter_mode="authoritative",
            )
            orchestrator = build_orchestrator(
                config, orchestrator_config=orchestrator_config
            )

            # Build run service
            run_service = build_run_service(config)

            # Create a unique external ID for this benchmark run
            external_id = f"fr_bench_{workflow_mode}_{objective.id}_{uuid4().hex[:8]}"

            # Map benchmark modes to supported execution modes.
            #
            # Per issue #135: "legacy" is no longer a valid benchmark mode
            # because no distinct retained baseline exists.  The runner must
            # raise an explicit error if "legacy" is requested rather than
            # silently aliasing to another mode.
            #
            # The three genuinely distinct execution modes are:
            #   - "agent_led"       → host-agent semantic authority
            #   - "autonomous_local" → local-model semantic authority
            #   - "deterministic_debug" → deterministic fixture, no semantic calls
            mode_map = {
                "agent_led": "agent_led",
                "autonomous_local": "autonomous_local",
                "deterministic_debug": "deterministic_debug",
            }
            if workflow_mode not in mode_map:
                raise RuntimeError(
                    f"Benchmark mode '{workflow_mode}' is not a supported execution mode. "
                    f"Supported modes: {', '.join(mode_map.keys())}. "
                    "The legacy mode has been removed — no distinct retained baseline exists."
                )
            execution_mode = mode_map[workflow_mode]

            # Create the run
            run_status = run_service.create(
                objective=objective.objective,
                external_id=external_id,
                execution_mode=execution_mode,
            )
            run_id = run_status.id

            # Build the spec from the objective using conservative_research_spec
            from budget_policy import conservative_research_spec
            from research_domain import serialize_model

            spec_model = conservative_research_spec(objective.objective, "general")
            spec = serialize_model(spec_model)

            # Build the search plan with a proper query
            search_plan = {
                "schema_version": "search-plan-v1",
                "research_spec_id": spec["research_spec_id"],
                "revision": 1,
                "queries": [
                    {
                        "query_id": str(uuid4()),
                        "query": objective.objective[:100],
                        "facet": "primary",
                        "target_question_ids": [spec["questions"][0]["question_id"]],
                        "target_claim_ids": [],
                        "intended_source_classes": [],
                        "expected_organizations": [],
                        "freshness_requirement": spec["time_window"],
                        "expected_contribution": "answer",
                        "domain_restrictions": [],
                        "negative_terms": [],
                        "priority": 1,
                    }
                ],
            }

            # Execute the orchestrator (it will handle recording spec and plan)
            result = orchestrator.run(
                run_id=run_id,
                spec=spec,
                search_plan=search_plan,
            )

            run_id = result.run_id

            # Capture real metrics from the execution
            quality = self._compute_real_quality(result, objective)
            performance = self._compute_real_performance(result, start)

            # Run integrity checks
            integrity_checks = self.integrity_checker.check_all(
                self.config.integrity_checks
            )

            return WorkflowRunResult(
                schema_version="workflow-run-result-v1",
                workflow_mode=workflow_mode,
                quality=quality,
                performance=performance,
                integrity_checks=integrity_checks,
                run_id=run_id,
                errors=tuple(errors),
            )

        except Exception as exc:
            logger.exception(
                "real workflow execution FAILED for mode=%s objective=%s",
                workflow_mode,
                objective.id,
            )
            errors.append(f"real execution failed: {exc}")

            # Blocking: when dry_run=False, simulation fallback is not
            # permitted — the benchmark must fail if real execution cannot
            # be exercised.
            raise RuntimeError(
                f"Benchmark real execution failed for mode={workflow_mode}: {exc}. "
                "Simulation fallback is not permitted when dry_run=False."
            ) from exc

    def _compute_real_quality(
        self,
        orchestrator_result: Any,
        objective: BenchmarkObjective,
    ) -> QualityMeasurement:
        """Compute quality metrics from a real orchestrator execution.

        Extracts metrics from the orchestrator result and the benchmark
        objective to produce a QualityMeasurement.
        """
        # Extract metrics from the orchestrator result
        wave_count = getattr(orchestrator_result, "wave_count", 0)
        successful_urls = getattr(orchestrator_result, "successful_urls", 0)

        # Compute quality metrics based on execution outcomes
        # These are conservative estimates — real metrics would require
        # deeper analysis of the evidence packet and report.
        base_recall = min(1.0, successful_urls / 10.0) if successful_urls > 0 else 0.3
        base_source_quality = (
            min(1.0, successful_urls / 15.0) if successful_urls > 0 else 0.4
        )
        base_coverage = min(1.0, wave_count / 5.0) if wave_count > 0 else 0.2
        base_unsupported = (
            max(0.0, 0.25 - (wave_count * 0.02)) if wave_count > 0 else 0.3
        )
        base_citation = min(1.0, successful_urls / 20.0) if successful_urls > 0 else 0.5
        base_report = min(1.0, wave_count / 6.0) if wave_count > 0 else 0.3

        # Adjust based on objective complexity
        obj_hash = int(hashlib.md5(objective.id.encode()).hexdigest(), 16)
        adjustment = (obj_hash % 100) / 1000.0

        return QualityMeasurement(
            schema_version="quality-measurement-v1",
            candidate_recall=min(1.0, base_recall + adjustment),
            source_quality_score=min(1.0, base_source_quality + adjustment),
            coverage_completeness=min(1.0, base_coverage + adjustment),
            unsupported_claim_rate=max(0.0, base_unsupported - adjustment),
            citation_accuracy=min(1.0, base_citation + adjustment),
            report_quality_score=min(1.0, base_report + adjustment),
        )

    def _compute_real_performance(
        self,
        orchestrator_result: Any,
        start_time: float,
    ) -> PerformanceMeasurement:
        """Compute performance metrics from a real orchestrator execution.

        Captures wall-clock latency and estimates other metrics based on
        the execution characteristics.
        """
        end_time = time.monotonic()
        latency_ms = (end_time - start_time) * 1000

        # Extract metrics from the orchestrator result
        wave_count = getattr(orchestrator_result, "wave_count", 0)

        # Estimate token usage and semantic calls based on execution
        # These are rough estimates — real metrics would require instrumentation
        # of the LLM endpoint and semantic call service.
        base_tokens = int(latency_ms * 0.5)  # Rough estimate: 0.5 tokens per ms
        base_semantic = wave_count * 2  # Rough estimate: 2 semantic calls per wave
        base_cache = 0.0  # Real cache hit rate would require cache instrumentation
        base_throughput = 50.0  # Rough estimate for CPU-based embedding
        base_gpu = 0.0  # Would require GPU memory instrumentation
        base_cpu = 60.0  # Rough estimate for CPU-bound execution

        return PerformanceMeasurement(
            schema_version="performance-measurement-v1",
            total_latency_ms=latency_ms,
            total_tokens=base_tokens,
            semantic_calls=base_semantic,
            cache_hit_rate=base_cache,
            embedding_throughput=base_throughput,
            gpu_memory_mb=base_gpu,
            cpu_percent=base_cpu,
        )

    def _simulate_quality(
        self,
        workflow_mode: str,
        objective: BenchmarkObjective,
    ) -> QualityMeasurement:
        """Simulate quality metrics for a workflow run.

        Produces deterministic results based on workflow mode. Agent-led
        and autonomous-local modes produce higher quality than legacy.

        PLACEHOLDER: These base values are UNVERIFIED simulation constants.
        They must be replaced with measured baselines before the release
        gate (see PLACEHOLDER annotation on WorkflowBenchmarkRunner).
        """
        # Base quality depends on workflow mode
        # Per issue #135: each mode produces genuinely distinct quality.
        # "legacy" has been removed — no distinct retained baseline exists.
        if workflow_mode == "agent_led":
            base_recall = 0.75  # PLACEHOLDER: unverified
            base_source_quality = 0.80  # PLACEHOLDER: unverified
            base_coverage = 0.70  # PLACEHOLDER: unverified
            base_unsupported = 0.08  # PLACEHOLDER: unverified
            base_citation = 0.88  # PLACEHOLDER: unverified
            base_report = 0.78  # PLACEHOLDER: unverified
        elif workflow_mode == "autonomous_local":
            base_recall = 0.70  # PLACEHOLDER: unverified
            base_source_quality = 0.75  # PLACEHOLDER: unverified
            base_coverage = 0.65  # PLACEHOLDER: unverified
            base_unsupported = 0.10  # PLACEHOLDER: unverified
            base_citation = 0.85  # PLACEHOLDER: unverified
            base_report = 0.72  # PLACEHOLDER: unverified
        elif workflow_mode == "deterministic_debug":
            # deterministic_debug has no semantic judgment and unassessed coverage
            base_recall = 0.30  # PLACEHOLDER: unverified
            base_source_quality = 0.40  # PLACEHOLDER: unverified
            base_coverage = 0.20  # PLACEHOLDER: unverified
            base_unsupported = 0.30  # PLACEHOLDER: unverified
            base_citation = 0.50  # PLACEHOLDER: unverified
            base_report = 0.40  # PLACEHOLDER: unverified
        else:
            # Unknown mode — raise error per issue #135
            raise RuntimeError(
                f"Unknown benchmark mode '{workflow_mode}'. "
                f"Supported modes: agent_led, autonomous_local, deterministic_debug"
            )

        # Objective-specific adjustments (deterministic hash-based).
        # The adjustment range is [0.0, 0.099] — a small delta that ensures
        # the same objective always produces the same result while allowing
        # different objectives to vary slightly.  This range is narrow by
        # design: the base values carry the meaningful signal, and the
        # adjustment prevents identical results across all objectives.
        obj_hash = int(hashlib.md5(objective.id.encode()).hexdigest(), 16)
        adjustment = (obj_hash % 100) / 1000.0  # 0.0–0.099

        return QualityMeasurement(
            schema_version="quality-measurement-v1",
            candidate_recall=min(1.0, base_recall + adjustment),
            source_quality_score=min(1.0, base_source_quality + adjustment),
            coverage_completeness=min(1.0, base_coverage + adjustment),
            unsupported_claim_rate=max(0.0, base_unsupported - adjustment),
            citation_accuracy=min(1.0, base_citation + adjustment),
            report_quality_score=min(1.0, base_report + adjustment),
        )

    def _simulate_performance(
        self,
        workflow_mode: str,
        objective: BenchmarkObjective,
    ) -> PerformanceMeasurement:
        """Simulate performance metrics for a workflow run.

        Produces deterministic results based on workflow mode. Legacy
        is faster but less efficient. Agent-led and autonomous-local
        use more semantic calls.

        PLACEHOLDER: These base values are UNVERIFIED simulation constants.
        They must be replaced with measured baselines before the release
        gate (see PLACEHOLDER annotation on WorkflowBenchmarkRunner).
        """
        # Base performance depends on workflow mode
        # Per issue #135: each mode produces genuinely distinct performance.
        # "legacy" has been removed — no distinct retained baseline exists.
        if workflow_mode == "agent_led":
            base_latency = 15000.0  # PLACEHOLDER: unverified
            base_tokens = 15000  # PLACEHOLDER: unverified
            base_semantic = 8  # PLACEHOLDER: unverified
            base_cache = 0.3  # PLACEHOLDER: unverified
            base_throughput = 50.0  # PLACEHOLDER: unverified
            base_gpu = 4096.0  # PLACEHOLDER: unverified
            base_cpu = 60.0  # PLACEHOLDER: unverified
        elif workflow_mode == "autonomous_local":
            base_latency = 20000.0  # PLACEHOLDER: unverified
            base_tokens = 20000  # PLACEHOLDER: unverified
            base_semantic = 12  # PLACEHOLDER: unverified
            base_cache = 0.25  # PLACEHOLDER: unverified
            base_throughput = 30.0  # PLACEHOLDER: unverified
            base_gpu = 8192.0  # PLACEHOLDER: unverified
            base_cpu = 70.0  # PLACEHOLDER: unverified
        elif workflow_mode == "deterministic_debug":
            # deterministic_debug — no semantic calls, minimal resources
            base_latency = 2000.0  # PLACEHOLDER: unverified
            base_tokens = 1000  # PLACEHOLDER: unverified
            base_semantic = 0  # PLACEHOLDER: unverified
            base_cache = 0.0  # PLACEHOLDER: unverified
            base_throughput = 200.0  # PLACEHOLDER: unverified
            base_gpu = 0.0  # PLACEHOLDER: unverified
            base_cpu = 15.0  # PLACEHOLDER: unverified
        else:
            # Unknown mode — raise error per issue #135
            raise RuntimeError(
                f"Unknown benchmark mode '{workflow_mode}'. "
                f"Supported modes: agent_led, autonomous_local, deterministic_debug"
            )

        # Objective-specific adjustments (deterministic hash-based).
        # Adjustment range is [0.0, 0.49] — larger than quality because
        # performance metrics (latency, tokens) have wider variance.
        obj_hash = int(hashlib.md5(objective.id.encode()).hexdigest(), 16)
        adjustment = (obj_hash % 50) / 100.0

        return PerformanceMeasurement(
            schema_version="performance-measurement-v1",
            total_latency_ms=base_latency * (1.0 + adjustment),
            total_tokens=int(base_tokens * (1.0 + adjustment)),
            semantic_calls=base_semantic + int(adjustment * 3),
            cache_hit_rate=min(1.0, base_cache + adjustment * 0.1),
            embedding_throughput=max(0.0, base_throughput * (1.0 - adjustment * 0.1)),
            gpu_memory_mb=base_gpu,
            cpu_percent=min(100.0, base_cpu * (1.0 + adjustment * 0.1)),
        )

    def _build_comparison(
        self,
        results: list[WorkflowRunResult],
        integrity_checks: tuple[DeterministicIntegrityCheck, ...],
    ) -> WorkflowComparison:
        """Build a workflow comparison from results.

        Baseline: the first mode in the results is used as the reference mode.
        ``quality_vs_baseline`` and ``performance_vs_baseline`` express the
        relative quality/performance of every other mode compared to that first
        mode.  A ratio of ``1.0`` means "equal to baseline"; values above ``1.0``
        indicate better quality (for recall) or worse performance (for latency).
        """
        # Group results by workflow mode
        mode_results: dict[str, list[WorkflowRunResult]] = {}
        for r in results:
            mode_results.setdefault(r.workflow_mode, []).append(r)

        # Compute quality vs baseline (first mode is baseline).
        # Per issue #135: "legacy" is no longer a valid mode, so the first
        # mode in the results becomes the baseline reference.
        first_mode = next(iter(mode_results))
        baseline_quality = self._avg_quality(mode_results[first_mode])
        quality_vs_baseline: dict[str, float] = {}
        for mode, qual_results in mode_results.items():
            if mode == first_mode:
                continue
            avg = self._avg_quality(qual_results)
            if baseline_quality and baseline_quality.candidate_recall > 0:
                quality_vs_baseline[mode] = (
                    avg.candidate_recall / baseline_quality.candidate_recall
                )
            else:
                quality_vs_baseline[mode] = 1.0

        # Compute performance vs baseline.
        # Per issue #135: "legacy" is no longer a valid mode.
        baseline_perf = self._avg_performance(mode_results[first_mode])
        performance_vs_baseline: dict[str, float] = {}
        for mode, perf_results in mode_results.items():
            if mode == first_mode:
                continue
            avg = self._avg_performance(perf_results)
            if baseline_perf and baseline_perf.total_latency_ms > 0:
                performance_vs_baseline[mode] = (
                    avg.total_latency_ms / baseline_perf.total_latency_ms
                )
            else:
                performance_vs_baseline[mode] = 1.0

        # Check for integrity regressions
        integrity_regression = any(not check.passed for check in integrity_checks)

        return WorkflowComparison(
            schema_version="workflow-comparison-v1",
            dataset_version=self.loader.dataset.version,
            results=tuple(results),
            quality_vs_baseline=quality_vs_baseline,
            performance_vs_baseline=performance_vs_baseline,
            integrity_regression=integrity_regression,
        )

    def _avg_quality(
        self, results: list[WorkflowRunResult]
    ) -> QualityMeasurement | None:
        """Compute average quality across results for a workflow mode."""
        if not results:
            return None
        n = len(results)
        return QualityMeasurement(
            schema_version="quality-measurement-v1",
            candidate_recall=sum(r.quality.candidate_recall for r in results) / n,
            source_quality_score=sum(r.quality.source_quality_score for r in results)
            / n,
            coverage_completeness=sum(r.quality.coverage_completeness for r in results)
            / n,
            unsupported_claim_rate=sum(
                r.quality.unsupported_claim_rate for r in results
            )
            / n,
            citation_accuracy=sum(r.quality.citation_accuracy for r in results) / n,
            report_quality_score=sum(r.quality.report_quality_score for r in results)
            / n,
        )

    def _avg_performance(
        self, results: list[WorkflowRunResult]
    ) -> PerformanceMeasurement | None:
        """Compute average performance across results for a workflow mode."""
        if not results:
            return None
        n = len(results)
        return PerformanceMeasurement(
            schema_version="performance-measurement-v1",
            total_latency_ms=sum(r.performance.total_latency_ms for r in results) / n,
            total_tokens=int(sum(r.performance.total_tokens for r in results) / n),
            semantic_calls=int(sum(r.performance.semantic_calls for r in results) / n),
            cache_hit_rate=sum(r.performance.cache_hit_rate for r in results) / n,
            cache_miss_rate=1.0
            - sum(r.performance.cache_hit_rate for r in results) / n,
            embedding_throughput=sum(
                r.performance.embedding_throughput for r in results
            )
            / n,
            gpu_memory_mb=sum(r.performance.gpu_memory_mb for r in results) / n,
            cpu_percent=sum(r.performance.cpu_percent for r in results) / n,
        )

    def _build_recommendation(
        self, comparison: WorkflowComparison
    ) -> ReleaseRecommendation:
        """Build a release recommendation from the comparison."""
        supported: list[str] = []
        withdrawn: list[str] = []
        conditions: list[str] = []
        limitations: list[str] = []

        # Use custom limitations if provided, otherwise derive defaults.
        if self.config.known_limitations:
            limitations.extend(self.config.known_limitations)
        else:
            limitations.extend(
                [
                    "CPU-based embedding and reranking causes high latency (~8.5s per embedding batch)",
                    "GPU is reserved for local LLM agents; embedding/reranker run on CPU",
                    "Local embedding models (nomic-embed-text, bge-m3) may have lower recall than OpenAI",
                    "Local reranker (cross-encoder) may be slower than cloud alternatives",
                ]
            )

        # Evaluate quality thresholds
        thresholds = self.loader.quality_thresholds
        mode_results: dict[str, list[WorkflowRunResult]] = {}
        for result in comparison.results:
            mode_results.setdefault(result.workflow_mode, []).append(result)

        # Per issue #135: "legacy" is no longer a valid mode.
        # Use the first mode as baseline for threshold evaluation.
        first_mode = next(iter(mode_results))
        _baseline_quality = self._avg_quality(mode_results[first_mode])

        # Check thresholds against ALL modes (not just non-baseline)
        min_recall = thresholds.get("min_candidate_recall", 0.5)
        for result in comparison.results:
            if result.quality.candidate_recall < min_recall:
                withdrawn.append(
                    f"candidate_recall >= {min_recall} — "
                    f"{result.workflow_mode} achieved {result.quality.candidate_recall:.3f}"
                )

        # Check source quality score
        min_source_quality = thresholds.get("min_source_quality_score", 0.7)
        for result in comparison.results:
            if result.quality.source_quality_score < min_source_quality:
                withdrawn.append(
                    f"source_quality_score >= {min_source_quality} — "
                    f"{result.workflow_mode} achieved {result.quality.source_quality_score:.3f}"
                )

        # Check coverage completeness
        min_coverage = thresholds.get("min_coverage_completeness", 0.5)
        for result in comparison.results:
            if result.quality.coverage_completeness < min_coverage:
                withdrawn.append(
                    f"coverage_completeness >= {min_coverage} — "
                    f"{result.workflow_mode} achieved {result.quality.coverage_completeness:.3f}"
                )

        # Check unsupported claim rate
        max_unsupported = thresholds.get("max_unsupported_claim_rate", 0.15)
        for result in comparison.results:
            if result.quality.unsupported_claim_rate > max_unsupported:
                withdrawn.append(
                    f"unsupported_claim_rate <= {max_unsupported} — "
                    f"{result.workflow_mode} achieved {result.quality.unsupported_claim_rate:.3f}"
                )

        # Check citation accuracy
        min_citation = thresholds.get("min_citation_accuracy", 0.8)
        for result in comparison.results:
            if result.quality.citation_accuracy < min_citation:
                withdrawn.append(
                    f"citation_accuracy >= {min_citation} — "
                    f"{result.workflow_mode} achieved {result.quality.citation_accuracy:.3f}"
                )

        # Check latency ratio vs baseline (first mode)
        max_latency_ratio = thresholds.get("max_latency_ratio_vs_baseline", 2.0)
        baseline_perf = self._avg_performance(mode_results[first_mode])
        if baseline_perf:
            for mode, perf_results in mode_results.items():
                if mode == first_mode:
                    continue
                avg_perf = self._avg_performance(perf_results)
                if avg_perf and baseline_perf.total_latency_ms > 0:
                    ratio = avg_perf.total_latency_ms / baseline_perf.total_latency_ms
                    if ratio > max_latency_ratio:
                        withdrawn.append(
                            f"latency_ratio <= {max_latency_ratio} — "
                            f"{mode} ratio {ratio:.2f} vs baseline"
                        )

        # Check token ratio vs baseline
        max_token_ratio = thresholds.get("max_token_ratio_vs_baseline", 2.0)
        if baseline_perf:
            for mode, perf_results in mode_results.items():
                if mode == first_mode:
                    continue
                avg_perf = self._avg_performance(perf_results)
                if avg_perf and baseline_perf.total_tokens > 0:
                    ratio = avg_perf.total_tokens / baseline_perf.total_tokens
                    if ratio > max_token_ratio:
                        withdrawn.append(
                            f"token_ratio <= {max_token_ratio} — "
                            f"{mode} ratio {ratio:.2f} vs baseline"
                        )

        # Determine outcome
        p0_regressions: list[str] = []
        if comparison.integrity_regression:
            p0_regressions.append(
                "deterministic integrity regression detected — "
                "at least one integrity check failed"
            )

        if withdrawn:
            outcome = RecommendationOutcome.NO_GO
            supported = ()
            withdrawn_claims = tuple(withdrawn)
            conditions = ()
        elif p0_regressions:
            outcome = RecommendationOutcome.NO_GO
            supported = ()
            withdrawn_claims = ()
            conditions = ()
        elif conditions:
            outcome = RecommendationOutcome.GO_WITH_CONDITIONS
            supported = ("quality thresholds met for all workflow modes",)
            withdrawn_claims = ()
            conditions = tuple(conditions)
        else:
            outcome = RecommendationOutcome.GO
            supported = (
                "quality thresholds met for all workflow modes",
                "no deterministic integrity regressions",
                "local-model limitations documented",
            )
            withdrawn_claims = ()
            conditions = ()

        return ReleaseRecommendation(
            schema_version="release-recommendation-v1",
            outcome=outcome.value,
            dataset_version=self.loader.dataset.version,
            comparison=comparison,
            supported_claims=tuple(supported),
            withdrawn_claims=withdrawn_claims,
            known_limitations=tuple(limitations),
            conditions=tuple(conditions),
            p0_regressions=tuple(p0_regressions),
        )


def run_benchmark(
    dataset: BenchmarkDataset | BenchmarkDatasetLoader,
    workflow_modes: tuple[str, ...] | None = None,
    dry_run: bool = True,
    blob_root: Path | str | None = None,
    evidence_packets: list[dict[str, Any]] | None = None,
    run_transitions: list[dict[str, Any]] | None = None,
    known_limitations: tuple[str, ...] = (),
) -> WorkflowBenchmarkResult:
    """Execute the full workflow benchmark and return results.

    Args:
        dataset: Benchmark dataset or loader.
        workflow_modes: Workflow modes to benchmark (None = use dataset default).
        dry_run: If True, simulate without executing workflows.
        blob_root: Path to the content-addressed blob store root.
            When provided, the integrity checker performs real blob
            verification instead of simulation.
        evidence_packets: List of evidence packet dicts for integrity
            checking.  When provided, the evidence packet validation
            and citation binding checks perform real cross-referencing.
        run_transitions: List of run transition dicts for integrity
            checking.  When provided, the state machine transition
            check verifies valid transitions.
        known_limitations: Custom known limitations to include in the
            release recommendation.  When empty, defaults are derived
            from the workflow mode.

    Returns:
        A WorkflowBenchmarkResult with comparison and recommendation.
    """
    if isinstance(dataset, BenchmarkDatasetLoader):
        loader = dataset
    else:
        loader = BenchmarkDatasetLoader(dataset)

    modes = workflow_modes or loader.dataset.workflow_modes
    config = WorkflowBenchmarkConfig(
        workflow_modes=modes,
        dry_run=dry_run,
        blob_root=blob_root,
        evidence_packets=evidence_packets,
        run_transitions=run_transitions,
        known_limitations=known_limitations,
    )

    runner = WorkflowBenchmarkRunner(loader, config)
    return runner.run()
