"""Release benchmark campaign domain contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar
from uuid import UUID

from ._common import _text


class WorkflowMode(str, Enum):
    """Workflow modes that the benchmark campaign compares."""

    LEGACY = "legacy"
    AGENT_LED = "agent_led"
    AUTONOMOUS_LOCAL = "autonomous_local"


class ClaimSupportLevel(str, Enum):
    """Support level for a claim in the benchmark evaluation."""

    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    UNSUPPORTED = "unsupported"
    UNTESTED = "untested"


class RecommendationOutcome(str, Enum):
    """Release recommendation outcomes."""

    GO = "go"
    GO_WITH_CONDITIONS = "go_with_conditions"
    NO_GO = "no_go"


@dataclass(frozen=True)
class BenchmarkSource:
    """A versioned source annotation referenced by a benchmark objective.

    ``source_class`` is mandatory so release source quality never infers
    source type from a URL or domain.
    """

    schema_version: str
    file_path: str
    relevance: bool
    role: str  # "relevant" | "distractor"
    source_class: str

    SCHEMA_VERSION = "benchmark-source-v2"

    def __post_init__(self) -> None:
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version: {self.schema_version}; "
                f"expected {self.SCHEMA_VERSION}"
            )
        _text(self.file_path, "benchmark_source.file_path")
        if self.role not in ("relevant", "distractor"):
            raise ValueError(
                f"role must be 'relevant' or 'distractor', got: {self.role}"
            )
        if self.relevance is not (self.role == "relevant"):
            raise ValueError(
                "benchmark_source.relevance must agree with benchmark_source.role"
            )
        _text(self.source_class, "benchmark_source.source_class")


@dataclass(frozen=True)
class BenchmarkObjective:
    """A single research objective in the benchmark dataset.

    Attributes:
        schema_version: ``"benchmark-objective-v2"``.
        id: Stable objective identifier (e.g., "obj-001").
        title: Human-readable title.
        objective: The research objective statement.
        questions: List of research questions.
        expected_source_classes: Expected classes of sources.
        known_relevant_sources: List of BenchmarkSource for relevant files.
        known_distractor_sources: List of BenchmarkSource for distractor files.
        search_queries: List of search query strings for the orchestrator.
        search_query_expected_sources: Mapping of search query to expected
            source file paths (subset of known_relevant_sources).
        ground_truth_answers: Mapping of question ID to expected answer text.
        expected_unresolved_controversies: Expected controversies.
        citation_support_labels: Question ID to support level mapping.
    """

    schema_version: str
    id: str
    title: str
    objective: str
    questions: tuple[str, ...]
    expected_source_classes: tuple[str, ...]
    known_relevant_sources: tuple[BenchmarkSource, ...]
    known_distractor_sources: tuple[BenchmarkSource, ...]
    expected_unresolved_controversies: tuple[str, ...]
    search_queries: tuple[str, ...] = ()
    search_query_expected_sources: dict[str, tuple[str, ...]] = field(
        default_factory=dict
    )
    ground_truth_answers: dict[str, str] = field(default_factory=dict)
    citation_support_labels: dict[str, str] = field(default_factory=dict)

    SCHEMA_VERSION = "benchmark-objective-v2"
    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = (
        "search_queries",
        "search_query_expected_sources",
        "ground_truth_answers",
        "citation_support_labels",
    )

    def __post_init__(self) -> None:
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version: {self.schema_version}; "
                f"expected {self.SCHEMA_VERSION}"
            )
        _text(self.id, "benchmark_objective.id")
        _text(self.title, "benchmark_objective.title")
        _text(self.objective, "benchmark_objective.objective")
        if not self.questions:
            raise ValueError("benchmark_objective.questions must not be empty")
        if not self.search_queries:
            raise ValueError("benchmark_objective.search_queries must not be empty")
        if not self.search_query_expected_sources:
            raise ValueError(
                "benchmark_objective.search_query_expected_sources must not be empty"
            )
        if not self.ground_truth_answers:
            raise ValueError(
                "benchmark_objective.ground_truth_answers must not be empty"
            )
        if not self.citation_support_labels:
            raise ValueError(
                "benchmark_objective.citation_support_labels must not be empty"
            )


@dataclass(frozen=True)
class BenchmarkDataset:
    """Versioned benchmark dataset for release campaigns.

    Attributes:
        schema_version: ``"benchmark-dataset-v2"``.
        version: Dataset version string.
        description: Human-readable description.
        evaluation_set: Whether this is the evaluation set (not for tuning).
        objectives: List of benchmark objectives.
        quality_thresholds: Quality thresholds for pass/fail.
        workflow_modes: Workflow modes to compare.
        deterministic_integrity_checks: List of integrity check names.
    """

    schema_version: str
    version: str
    description: str
    evaluation_set: bool
    objectives: tuple[BenchmarkObjective, ...]
    quality_thresholds: dict[str, float | bool]
    workflow_modes: tuple[str, ...]
    deterministic_integrity_checks: tuple[str, ...]

    SCHEMA_VERSION = "benchmark-dataset-v2"

    def __post_init__(self) -> None:
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version: {self.schema_version}; "
                f"expected {self.SCHEMA_VERSION}"
            )
        _text(self.version, "benchmark_dataset.version")
        _text(self.description, "benchmark_dataset.description")
        if not self.objectives:
            raise ValueError("benchmark_dataset.objectives must not be empty")
        for mode in self.workflow_modes:
            if mode not in (
                "legacy",
                "agent_led",
                "autonomous_local",
                "deterministic_debug",
            ):
                raise ValueError(
                    f"workflow_modes must be one of legacy, agent_led, "
                    f"autonomous_local, deterministic_debug; got: {mode}"
                )


@dataclass(frozen=True)
class QualityMeasurement:
    """Quality metric measurement for a single workflow run.

    Attributes:
        schema_version: ``"quality-measurement-v3"`` for status-aware release
            measurements. Earlier versions remain readable.
        candidate_recall: Fraction of relevant candidates retrieved.
        source_quality_score: Quality score of sources (0.0–1.0).
        coverage_completeness: Fraction of coverage items resolved.
        unsupported_claim_rate: Fraction of unsupported claims.
        citation_accuracy: Fraction of correct citations.
        report_quality_score: Blinded review report quality (0.0–1.0).
    """

    schema_version: str
    candidate_recall: float | None
    source_quality_score: float | None
    coverage_completeness: float | None
    unsupported_claim_rate: float | None
    citation_accuracy: float | None
    report_quality_score: float | None

    # Backward-compatible attribute used by the schema registry.
    # The v3 schema is the current write version; v1 and v2 remain readable.
    SCHEMA_VERSION = "quality-measurement-v3"
    SCHEMA_VERSIONS = (
        "quality-measurement-v1",
        "quality-measurement-v2",
        "quality-measurement-v3",
    )

    def __post_init__(self) -> None:
        if self.schema_version not in self.SCHEMA_VERSIONS:
            raise ValueError(
                f"unsupported schema_version: {self.schema_version}. "
                f"Allowed: {self.SCHEMA_VERSIONS}"
            )
        for field_name, value in [
            ("candidate_recall", self.candidate_recall),
            ("source_quality_score", self.source_quality_score),
            ("coverage_completeness", self.coverage_completeness),
            ("unsupported_claim_rate", self.unsupported_claim_rate),
            ("citation_accuracy", self.citation_accuracy),
            ("report_quality_score", self.report_quality_score),
        ]:
            if value is not None and not (0.0 <= value <= 1.0):
                raise ValueError(
                    f"{field_name} must be between 0.0 and 1.0, got: {value}"
                )


@dataclass(frozen=True)
class PerformanceMeasurement:
    """Performance metric measurement for a single workflow run.

    Attributes:
        schema_version: ``"performance-measurement-v2"`` for status-aware
            release measurements. Version 1 remains readable.
        total_latency_ms: Total wall-clock latency in milliseconds.
        total_tokens: Total tokens consumed.
        semantic_calls: Number of semantic (LLM) calls made.
        cache_hit_rate: Fraction of cache hits (0.0–1.0).
        cache_miss_rate: Fraction of cache misses.  The dataclass accepts
            ``None`` as the default and auto-computes
            ``cache_miss_rate = 1.0 - cache_hit_rate`` in ``__post_init__``.
            Callers only need to set ``cache_hit_rate`` — the miss rate is
            derived deterministically. It remains ``None`` when the hit rate
            is unavailable.
        embedding_throughput: Embeddings per second.
        gpu_memory_mb: Mean run-window GPU memory in MB, or ``None`` when
            unavailable.
        cpu_percent: Peak CPU usage (0.0–100.0).
    """

    schema_version: str
    total_latency_ms: float
    total_tokens: int | None
    semantic_calls: int
    cache_hit_rate: float | None
    embedding_throughput: float | None
    gpu_memory_mb: float | None
    cpu_percent: float | None
    cache_miss_rate: float | None = None

    SCHEMA_VERSION = "performance-measurement-v2"
    SCHEMA_VERSIONS = ("performance-measurement-v1", "performance-measurement-v2")

    def __post_init__(self) -> None:
        if self.schema_version not in self.SCHEMA_VERSIONS:
            raise ValueError(
                f"unsupported schema_version: {self.schema_version}. "
                f"Allowed: {self.SCHEMA_VERSIONS}"
            )
        if self.total_latency_ms < 0:
            raise ValueError("total_latency_ms must be >= 0")
        if self.total_tokens is not None and self.total_tokens < 0:
            raise ValueError("total_tokens must be >= 0")
        if self.semantic_calls < 0:
            raise ValueError("semantic_calls must be >= 0")
        if self.cache_hit_rate is not None and not (0.0 <= self.cache_hit_rate <= 1.0):
            raise ValueError("cache_hit_rate must be between 0.0 and 1.0")
        # Auto-compute cache_miss_rate from cache_hit_rate when not provided.
        # This ensures callers only need to set cache_hit_rate and the
        # miss rate is derived deterministically.
        expected_miss = (
            None if self.cache_hit_rate is None else 1.0 - self.cache_hit_rate
        )
        if expected_miss is None:
            object.__setattr__(self, "cache_miss_rate", None)
        elif (
            self.cache_miss_rate is None
            or abs(self.cache_miss_rate - expected_miss) > 1e-9
        ):
            object.__setattr__(self, "cache_miss_rate", expected_miss)
        if self.embedding_throughput is not None and self.embedding_throughput < 0:
            raise ValueError("embedding_throughput must be >= 0")
        if self.gpu_memory_mb is not None and self.gpu_memory_mb < 0:
            raise ValueError("gpu_memory_mb must be >= 0")
        if self.cpu_percent is not None and not (0.0 <= self.cpu_percent <= 100.0):
            raise ValueError("cpu_percent must be between 0.0 and 100.0")


@dataclass(frozen=True)
class DeterministicIntegrityCheck:
    """Result of a single deterministic integrity check.

    Attributes:
        schema_version: Always ``"integrity-check-v1"``.
        check_name: Name of the integrity check.
        passed: Whether the check passed.
        details: Human-readable details about the check result.
    """

    schema_version: str
    check_name: str
    passed: bool
    details: str

    SCHEMA_VERSION = "integrity-check-v1"

    def __post_init__(self) -> None:
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        _text(self.check_name, "integrity_check.check_name")
        _text(self.details, "integrity_check.details")


@dataclass(frozen=True)
class WorkflowRunResult:
    """Result of running a single workflow mode in the benchmark.

    Attributes:
        schema_version: Always ``"workflow-run-result-v1"``.
        workflow_mode: The workflow mode that was run.
        quality: Quality measurements.
        performance: Performance measurements.
        integrity_checks: Deterministic integrity check results.
        run_id: The research run ID (None for dry-run).
        errors: Any errors encountered during the run.
        quality_metrics: Detailed quality metric records with status.
        performance_metrics: Detailed performance metric records with status.
    """

    schema_version: str
    workflow_mode: str
    quality: QualityMeasurement
    performance: PerformanceMeasurement
    integrity_checks: tuple[DeterministicIntegrityCheck, ...]
    run_id: UUID | None
    errors: tuple[str, ...]
    quality_metrics: tuple[object, ...] = ()
    performance_metrics: tuple[object, ...] = ()

    SCHEMA_VERSION = "workflow-run-result-v1"

    def __post_init__(self) -> None:
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if self.workflow_mode not in (
            "legacy",
            "agent_led",
            "autonomous_local",
            "deterministic_debug",
        ):
            raise ValueError(
                f"workflow_mode must be one of legacy, agent_led, "
                f"autonomous_local, deterministic_debug; got: {self.workflow_mode}"
            )


@dataclass(frozen=True)
class WorkflowComparison:
    """Side-by-side comparison of workflow modes.

    Attributes:
        schema_version: Always ``"workflow-comparison-v1"``.
        dataset_version: Version of the benchmark dataset used.
        results: Per-workflow-mode results.
        quality_vs_baseline: Quality comparison against baseline.
        performance_vs_baseline: Performance comparison against baseline.
        integrity_regression: Whether any P0 integrity regression detected.
    """

    schema_version: str
    dataset_version: str
    results: tuple[WorkflowRunResult, ...]
    quality_vs_baseline: dict[str, float]
    performance_vs_baseline: dict[str, float]
    integrity_regression: bool

    SCHEMA_VERSION = "workflow-comparison-v1"

    def __post_init__(self) -> None:
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if not self.results:
            raise ValueError("workflow_comparison.results must not be empty")
        modes = {r.workflow_mode for r in self.results}
        if len(modes) < 2:
            raise ValueError(
                "workflow_comparison.results must include at least 2 workflow modes"
            )


@dataclass(frozen=True)
class ReleaseRecommendation:
    """Release recommendation produced by the benchmark campaign.

    Attributes:
        schema_version: Always ``"release-recommendation-v1"``.
        outcome: GO, GO_WITH_CONDITIONS, or NO_GO.
        dataset_version: Version of the benchmark dataset used.
        comparison: The workflow comparison results.
        supported_claims: Claims that the benchmark supports.
        withdrawn_claims: Claims that the benchmark does not support.
        known_limitations: Documented local-model and infrastructure limitations.
        conditions: Conditions for GO_WITH_CONDITIONS.
        p0_regressions: Any P0 deterministic-integrity regressions found.
    """

    schema_version: str
    outcome: str
    dataset_version: str
    comparison: WorkflowComparison
    supported_claims: tuple[str, ...]
    withdrawn_claims: tuple[str, ...]
    known_limitations: tuple[str, ...]
    conditions: tuple[str, ...]
    p0_regressions: tuple[str, ...]

    SCHEMA_VERSION = "release-recommendation-v1"

    def __post_init__(self) -> None:
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if self.outcome not in ("go", "go_with_conditions", "no_go"):
            raise ValueError(
                f"outcome must be go, go_with_conditions, or no_go; got: {self.outcome}"
            )
        if self.outcome == "go" and self.withdrawn_claims:
            raise ValueError("outcome cannot be 'go' when there are withdrawn claims")
        if self.outcome == "go" and self.p0_regressions:
            raise ValueError("outcome cannot be 'go' when there are P0 regressions")
        if self.outcome == "go_with_conditions" and not self.conditions:
            raise ValueError("outcome is 'go_with_conditions' but conditions is empty")
