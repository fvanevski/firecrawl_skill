"""Measured release benchmark for issue #135.

This module provides:

* ``MetricEngine`` — reads quality and performance metrics from persisted
  PostgreSQL artifacts (search candidates, semantic calls, coverage events,
  evidence packets, cache records).  All metrics are derived from real state,
  never from formulas or simulation.
* ``ReleaseBenchmarkConfig`` — configuration for a release-mode benchmark run.
* ``ReleaseBenchmarkResult`` — structured output with per-mode metrics,
  campaign metadata, and reproducibility comparison.
* ``ReleaseBenchmarkRunner`` — orchestrates real campaign execution across
  genuinely distinct workflow modes, computes metrics from persisted state,
  and produces a release-ready comparison report.

Usage
-----
    >>> from research_store.release.benchmark import (
    ...     ReleaseBenchmarkConfig,
    ...     ReleaseBenchmarkRunner,
    ...     load_benchmark_dataset,
    ... )
    >>> dataset = load_benchmark_dataset("tests/fixtures/benchmark/benchmark-v2.json")
    >>> config = ReleaseBenchmarkConfig(
    ...     database_url="postgresql://...",
    ...     blob_root=Path("/tmp/benchmark-blobs"),
    ... )
    >>> runner = ReleaseBenchmarkRunner(dataset, config)
    >>> result = runner.run()
    >>> print(result.campaign_id)
"""

from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

try:
    import psutil  # type: ignore[import-not-found,import-untyped]

    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

try:
    import pynvml  # type: ignore[import-not-found,import-untyped]

    _HAS_PYNVML = True
except ImportError:
    _HAS_PYNVML = False

from research_domain.models import (
    BenchmarkDataset,
    BenchmarkObjective,
    DeterministicIntegrityCheck,
    PerformanceMeasurement,
    QualityMeasurement,
    RecommendationOutcome,
    ReleaseRecommendation,
    WorkflowComparison,
    WorkflowRunResult,
)

from .workflow import BenchmarkDatasetLoader, DeterministicIntegrityChecker

logger = logging.getLogger(__name__)


class MetricStatus(str, enum.Enum):
    """Availability/completeness state of a single metric."""

    MEASURED = "measured"
    UNAVAILABLE = "unavailable"
    INCOMPLETE = "incomplete"
    UNEVALUATED = "unevaluated"
    STALE = "stale"
    INVALID = "invalid"
    NOT_APPLICABLE = "not_applicable"


MANDATORY_QUALITY_METRICS = frozenset(
    {
        "candidate_recall",
        "source_quality_score",
        "coverage_completeness",
        "unsupported_claim_rate",
        "citation_accuracy",
        "report_quality_score",
    }
)
MANDATORY_PERFORMANCE_METRICS = frozenset(
    {
        "total_tokens",
        "cache_hit_rate",
        "embedding_throughput",
        "cpu_percent",
        "gpu_memory_mb",
    }
)
STRICT_ACCEPTABLE_QUALITY_STATUSES = frozenset({MetricStatus.MEASURED})
STRICT_ACCEPTABLE_PERFORMANCE_STATUSES = frozenset(
    {MetricStatus.MEASURED, MetricStatus.NOT_APPLICABLE}
)
STRICT_MIN_CPU_SAMPLES = 2
STRICT_MIN_GPU_SAMPLES = 3

RELEASE_MODES = (
    "agent_led",
    "autonomous_local",
    "deterministic_debug",
)
RELEASE_CACHE_STAGE_SET_VERSION = "release-cache-stages-v1"
RELEASE_CACHE_STAGES = (
    "outline",
    "binding",
    "draft",
    "citation_pass",
    "indexing",
)
REPRODUCIBILITY_POLICY_VERSION = "reproducibility-policy-v2"
OPERATIONAL_PERFORMANCE_METRICS = frozenset(
    {
        "total_latency_ms",
        "embedding_throughput",
        "cpu_percent",
        "gpu_memory_mb",
    }
)
OPERATIONAL_ABSOLUTE_TOLERANCES = {
    "cpu_percent": 2.0,
    "gpu_memory_mb": 256.0,
}
LEGACY_MODE_FORBIDDEN = True
COVERAGE_EVENT_STATUS_CHANGED = "item_status_changed"
COVERAGE_EVENT_CREATED = "item_created"


def _canonical_match(file_path: str, canonical_url: str) -> bool:
    """Check whether a benchmark file path matches a candidate canonical URL."""
    from urllib.parse import urlparse

    fp = file_path.strip().rstrip("/")
    cu = canonical_url.strip().removeprefix("file://")
    if fp == cu:
        return True
    parsed = urlparse(cu)
    path = parsed.path
    if path:
        path = path.replace("\\", "/").rstrip("/")
        if fp == path or path.endswith("/" + fp):
            return True
        if "/" + fp + "/" in ("/" + path + "/"):
            return True
    if parsed.query and fp in parsed.query:
        return True
    return bool(parsed.fragment and fp in parsed.fragment)


def _annotated_source_quality(
    candidates: list[tuple[str, str]],
    objective: BenchmarkObjective | None,
) -> tuple[float | None, str, MetricStatus]:
    """Measure source quality from versioned benchmark annotations."""
    if not candidates:
        return None, "unavailable — no acquired candidates", MetricStatus.UNAVAILABLE
    if objective is None or not objective.expected_source_classes:
        return (
            None,
            "unavailable — no versioned expected source classes",
            MetricStatus.UNAVAILABLE,
        )
    expected_classes = set(objective.expected_source_classes)
    relevant_sources = tuple(
        source
        for source in objective.known_relevant_sources
        if source.role == "relevant"
    )
    distractor_sources = tuple(
        source
        for source in objective.known_distractor_sources
        if source.role == "distractor"
    )
    missing_annotations = sorted(
        source.file_path for source in relevant_sources if not source.source_class
    )
    annotated_classes = {
        source.source_class for source in relevant_sources if source.source_class
    }
    undeclared_classes = sorted(annotated_classes - expected_classes)
    unrepresented_classes = sorted(expected_classes - annotated_classes)
    if missing_annotations or undeclared_classes or unrepresented_classes:
        return (
            None,
            (
                "invalid source annotation contract — "
                f"missing_annotations={missing_annotations}, "
                f"undeclared_classes={undeclared_classes}, "
                f"unrepresented_classes={unrepresented_classes}"
            ),
            MetricStatus.INVALID,
        )
    relevant_hits = 0
    distractor_hits = 0
    unclassified_hits = 0
    acquired_classes: set[str] = set()
    for canonical_url, _domain in candidates:
        relevant = next(
            (
                source
                for source in relevant_sources
                if _canonical_match(source.file_path, canonical_url)
            ),
            None,
        )
        if relevant is not None:
            relevant_hits += 1
            acquired_classes.add(relevant.source_class)
            continue
        if any(
            _canonical_match(source.file_path, canonical_url)
            for source in distractor_sources
        ):
            distractor_hits += 1
        else:
            unclassified_hits += 1
    labeled_precision = relevant_hits / len(candidates)
    class_coverage = len(acquired_classes) / len(expected_classes)
    score = (
        2.0 * labeled_precision * class_coverage / (labeled_precision + class_coverage)
        if labeled_precision + class_coverage > 0
        else 0.0
    )
    formula = (
        "source-quality-v2 harmonic_mean("
        f"labeled_precision={relevant_hits}/{len(candidates)}, "
        f"required_class_coverage={len(acquired_classes)}/{len(expected_classes)}"
        "); "
        f"distractor_candidates={distractor_hits}, "
        f"unclassified_candidates={unclassified_hits}"
    )
    return round(score, 6), formula, MetricStatus.MEASURED


@dataclass(frozen=True)
class MetricSource:
    table: str
    column: str
    run_id: str
    method: str
    event_ids: tuple[str, ...] = ()
    stages: tuple[str, ...] = ()
    stage_set_version: str = ""
    sample_count: int = 0
    device_type: str = ""
    device_index: int | None = None
    device_uuid: str = ""
    collector: str = ""
    collector_version: str = ""
    status_counts: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class QualityMetric:
    name: str
    value: float | None
    source: MetricSource
    formula: str
    status: MetricStatus = MetricStatus.MEASURED


@dataclass(frozen=True)
class PerformanceMetric:
    name: str
    value: float | None
    source: MetricSource
    formula: str
    status: MetricStatus = MetricStatus.MEASURED


@dataclass(frozen=True)
class ReleaseBenchmarkConfig:
    database_url: str = ""
    blob_root: Path | str | None = None
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    host_artifact_supplier: Any = None
    execution_modes: tuple[str, ...] = RELEASE_MODES
    objective_ids: tuple[str, ...] | None = None
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
    known_limitations: tuple[str, ...] = ()
    reproducibility_tolerance: float = 0.15
    operational_reproducibility_ratio_limit: float = 2.0
    strict: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.reproducibility_tolerance <= 1.0:
            raise ValueError("reproducibility_tolerance must be between 0 and 1")
        if self.operational_reproducibility_ratio_limit < 1.0:
            raise ValueError("operational_reproducibility_ratio_limit must be >= 1")


class MetricEngine:
    """Extract quality and performance metrics from persisted PostgreSQL state."""

    def __init__(
        self, database_url: str, config: ReleaseBenchmarkConfig | None = None
    ) -> None:
        self.database_url = database_url
        self.config = config
        # psycopg is an optional runtime dependency imported dynamically. Keep the
        # handle explicitly dynamic while preserving fail-closed connection checks.
        self._connection: Any = None

    def connect(self) -> None:
        try:
            import psycopg

            self._connection = psycopg.connect(self.database_url)
        except ImportError:
            raise RuntimeError(
                "psycopg is required for metric extraction. Install with: pip install psycopg"
            )

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> MetricEngine:  # noqa: PYI034
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def extract_quality_metrics(
        self, run_id: UUID, objective: BenchmarkObjective | None = None
    ) -> tuple[QualityMeasurement, tuple[QualityMetric, ...]]:
        if self._connection is None:
            raise RuntimeError(
                "MetricEngine not connected. Call connect() first or use as context manager."
            )
        relevant_paths: set[str] = set()
        if objective is not None:
            relevant_paths = {
                src.file_path
                for src in objective.known_relevant_sources
                if src.role == "relevant"
            }
        with self._connection.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT canonical_url, domain
                   FROM search_candidates
                   WHERE run_id = %s""",
                (run_id,),
            )
            candidates = cur.fetchall()
        candidate_count = len(candidates)
        matched_relevant: set[str] = set()
        for url, _domain in candidates:
            for path in relevant_paths:
                if _canonical_match(path, url):
                    matched_relevant.add(path)
                    break
        total_relevant = len(relevant_paths)
        tp = len(matched_relevant)
        if total_relevant > 0 and candidate_count > 0:
            candidate_recall = tp / total_relevant
            recall_formula = (
                f"TP={tp} / (TP+FN={total_relevant}) — "
                "labeled-source recall against benchmark ground truth"
            )
            recall_source_table = "benchmark_ground_truth"
        else:
            candidate_recall = None
            recall_formula = (
                "unavailable — no acquired candidates"
                if candidate_count == 0
                else "unavailable — no versioned labeled relevant set"
            )
            recall_source_table = (
                "search_candidates"
                if candidate_count == 0
                else "benchmark_ground_truth"
            )
        source_quality, source_quality_formula, source_quality_status = (
            _annotated_source_quality(candidates, objective)
        )
        with self._connection.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT ON (item_id)
                      item_id, new_status
                   FROM coverage_events
                   WHERE run_id = %s
                     AND event_type IN (%s, %s)
                   ORDER BY item_id, coverage_revision DESC, created_at DESC""",
                (run_id, COVERAGE_EVENT_STATUS_CHANGED, COVERAGE_EVENT_CREATED),
            )
            item_statuses = cur.fetchall()
        satisfied_statuses = {"satisfied", "partially_supported"}
        applicable_statuses = {
            "satisfied",
            "partially_supported",
            "contradicted",
            "qualified",
            "supported",
            "blocked",
            "waived",
            "unassessed",
        }
        satisfied_count = 0
        applicable_count = 0
        for _item_id, status in item_statuses:
            if status in applicable_statuses:
                applicable_count += 1
                if status in satisfied_statuses:
                    satisfied_count += 1
        if applicable_count > 0:
            coverage_completeness = satisfied_count / applicable_count
            coverage_formula = (
                f"satisfied({satisfied_count}) / applicable({applicable_count})"
            )
        else:
            coverage_completeness = None
            coverage_formula = "unavailable — no applicable coverage items"
        with self._connection.cursor() as cur:
            cur.execute(
                """SELECT semantic_status, COUNT(*)
                   FROM research_claims
                   WHERE run_id = %s
                   GROUP BY semantic_status""",
                (run_id,),
            )
            claim_status_counts = dict(cur.fetchall())
        total_claims = sum(claim_status_counts.values()) if claim_status_counts else 0
        unsupported_count = claim_status_counts.get("unsupported", 0)
        unassessed_count = claim_status_counts.get("unassessed", 0)
        assessed_claims = total_claims - unassessed_count
        unsupported_claim_status = MetricStatus.MEASURED
        if assessed_claims > 0:
            unsupported_claim_rate = unsupported_count / assessed_claims
            unsupported_formula = (
                f"unsupported({unsupported_count}) / assessed({assessed_claims})"
            )
        else:
            unsupported_claim_status = MetricStatus.UNAVAILABLE
            unsupported_claim_rate = None
            unsupported_formula = "unavailable — no assessed claims"

        citation_pass_valid = 0
        citation_pass_total = 0
        citation_accuracy_status = MetricStatus.UNAVAILABLE
        citation_formula = "unavailable — no citation_pass artifact"
        citation_source_table = "synthesis_stages"
        citation_source_column = "artifact.validation_results"
        citation_source_method = "valid_over_validation_results"
        with self._connection.cursor() as cur:
            cur.execute(
                """SELECT artifact FROM synthesis_stages
                   WHERE run_id = %s AND stage_name = 'citation_pass'
                     AND stage_status = 'completed'
                   ORDER BY updated_at DESC LIMIT 1""",
                (run_id,),
            )
            row = cur.fetchone()
        if row is not None and row[0] is not None:
            artifact = row[0]
            if isinstance(artifact, str):
                import json

                try:
                    artifact = json.loads(artifact)
                except (json.JSONDecodeError, TypeError):
                    artifact = None
            if isinstance(artifact, dict):
                validation_results = artifact.get("validation_results", [])
                if isinstance(validation_results, list) and validation_results:
                    citation_pass_total = len(validation_results)
                    citation_pass_valid = sum(
                        1
                        for vr in validation_results
                        if isinstance(vr, dict) and vr.get("status") == "valid"
                    )
                    citation_formula = (
                        f"valid_citations({citation_pass_valid}) / "
                        f"total_validation_results({citation_pass_total})"
                    )
                    citation_accuracy_status = MetricStatus.MEASURED
        if citation_pass_total == 0:
            with self._connection.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(DISTINCT c.claim_id) AS total_assessed,
                              COUNT(DISTINCT cl.claim_id) AS with_evidence
                           FROM research_claims c
                           LEFT JOIN claim_evidence_links cl
                             ON c.claim_id = cl.claim_id AND c.run_id = cl.run_id
                           WHERE c.run_id = %s
                             AND c.semantic_status != 'unassessed'""",
                    (run_id,),
                )
                citation_row = cur.fetchone()
            total_assessed = int(citation_row[0] or 0) if citation_row else 0
            with_evidence = int(citation_row[1] or 0) if citation_row else 0
            if total_assessed > 0:
                citation_pass_total = total_assessed
                citation_pass_valid = with_evidence
                citation_accuracy = with_evidence / total_assessed
                citation_formula = (
                    f"with_evidence({with_evidence}) / assessed({total_assessed})"
                )
                citation_source_table = "claim_evidence_links"
                citation_source_column = "claim_id"
                citation_source_method = "claims_with_evidence_over_assessed"
                citation_accuracy_status = MetricStatus.MEASURED
            else:
                citation_accuracy = None
                citation_formula = "unavailable — no assessed claims with evidence"
                citation_source_table = "claim_evidence_links"
                citation_source_column = "claim_id"
                citation_source_method = "claims_with_evidence_over_assessed"
        else:
            citation_accuracy = citation_pass_valid / citation_pass_total
            citation_accuracy = round(citation_accuracy, 6)
        supported_count = claim_status_counts.get("supported", 0)
        contradicted_count = claim_status_counts.get("contradicted", 0)
        qualified_count = claim_status_counts.get("qualified", 0)
        claim_support_rate = (
            (supported_count + contradicted_count + qualified_count) / assessed_claims
            if assessed_claims > 0
            else None
        )
        coverage_weight = 0.30
        citation_weight = 0.30
        support_weight = 0.25
        packet_weight = 0.15
        with self._connection.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM evidence_packets WHERE run_id = %s", (run_id,)
            )
            packet_row = cur.fetchone()
        packet_count = int(packet_row[0] or 0) if packet_row else 0
        packet_present = 1.0 if packet_count > 0 else 0.0
        validation_support_rate: float | None = None
        with self._connection.cursor() as cur:
            cur.execute(
                """SELECT artifact FROM synthesis_stages
                   WHERE run_id = %s AND stage_name = 'validation'
                     AND stage_status IN ('completed', 'failed')
                   ORDER BY updated_at DESC LIMIT 1""",
                (run_id,),
            )
            val_row = cur.fetchone()
        if val_row is not None and val_row[0] is not None:
            val_artifact = val_row[0]
            if isinstance(val_artifact, str):
                import json

                try:
                    val_artifact = json.loads(val_artifact)
                except (json.JSONDecodeError, TypeError):
                    val_artifact = None
            if isinstance(val_artifact, dict):
                claim_manifest = val_artifact.get("claim_manifest", [])
                if isinstance(claim_manifest, list) and claim_manifest:
                    resolved = 0
                    positive = 0
                    for claim_record in claim_manifest:
                        if not isinstance(claim_record, dict):
                            continue
                        resolution = claim_record.get("resolution", "")
                        if resolution in (
                            "supported",
                            "contradicted",
                            "qualified",
                            "unsupported",
                            "unassessed",
                        ):
                            resolved += 1
                            if resolution in ("supported", "contradicted", "qualified"):
                                positive += 1
                    if resolved > 0:
                        validation_support_rate = positive / resolved
        effective_support_rate = (
            validation_support_rate
            if validation_support_rate is not None
            else claim_support_rate
        )
        report_inputs_complete = (
            coverage_completeness is not None
            and citation_accuracy is not None
            and effective_support_rate is not None
            and packet_count > 0
        )
        report_quality = None
        if report_inputs_complete:
            report_quality = (
                coverage_weight * coverage_completeness
                + citation_weight * citation_accuracy
                + support_weight * effective_support_rate
                + packet_weight * packet_present
            )
            report_quality = min(1.0, max(0.0, report_quality))
        report_quality_formula = (
            f"coverage({coverage_weight}) + citation({citation_weight}) + "
            f"support({support_weight}) + packet({packet_weight})"
        )
        quality = QualityMeasurement(
            schema_version="quality-measurement-v3",
            candidate_recall=round(candidate_recall, 6)
            if candidate_recall is not None
            else None,
            source_quality_score=round(source_quality, 6)
            if source_quality is not None
            else None,
            coverage_completeness=round(coverage_completeness, 6)
            if coverage_completeness is not None
            else None,
            unsupported_claim_rate=round(unsupported_claim_rate, 6)
            if unsupported_claim_rate is not None
            else None,
            citation_accuracy=round(citation_accuracy, 6)
            if citation_accuracy is not None
            else None,
            report_quality_score=round(report_quality, 6)
            if report_quality is not None
            else None,
        )
        recall_status = (
            MetricStatus.UNAVAILABLE
            if candidate_recall is None
            else MetricStatus.MEASURED
        )
        coverage_status = (
            MetricStatus.UNAVAILABLE if applicable_count == 0 else MetricStatus.MEASURED
        )
        metrics = (
            QualityMetric(
                name="candidate_recall",
                value=quality.candidate_recall,
                source=MetricSource(
                    table=recall_source_table,
                    column="known_relevant_sources",
                    run_id=str(run_id),
                    method="canonical_identity_match",
                ),
                formula=recall_formula,
                status=recall_status,
            ),
            QualityMetric(
                name="source_quality_score",
                value=quality.source_quality_score,
                source=MetricSource(
                    table="benchmark_ground_truth + search_candidates",
                    column="known_sources.source_class + canonical_url",
                    run_id=str(run_id),
                    method="annotated_precision_class_coverage_hmean_v2",
                ),
                formula=source_quality_formula,
                status=source_quality_status,
            ),
            QualityMetric(
                name="coverage_completeness",
                value=quality.coverage_completeness,
                source=MetricSource(
                    table="coverage_events",
                    column="item_status (latest revision)",
                    run_id=str(run_id),
                    method="satisfied_over_applicable",
                ),
                formula=coverage_formula,
                status=coverage_status,
            ),
            QualityMetric(
                name="unsupported_claim_rate",
                value=quality.unsupported_claim_rate,
                source=MetricSource(
                    table="research_claims",
                    column="semantic_status",
                    run_id=str(run_id),
                    method="unsupported_over_assessed",
                ),
                formula=unsupported_formula,
                status=unsupported_claim_status,
            ),
            QualityMetric(
                name="citation_accuracy",
                value=quality.citation_accuracy,
                source=MetricSource(
                    table=citation_source_table,
                    column=citation_source_column,
                    run_id=str(run_id),
                    method=citation_source_method,
                ),
                formula=citation_formula,
                status=citation_accuracy_status,
            ),
            QualityMetric(
                name="report_quality_score",
                value=quality.report_quality_score,
                source=MetricSource(
                    table="evidence_packets",
                    column="COUNT(*) + coverage + claims + evidence_links",
                    run_id=str(run_id),
                    method="versioned_rubric_v1",
                ),
                formula=report_quality_formula,
                status=(
                    MetricStatus.MEASURED
                    if report_inputs_complete
                    else MetricStatus.INCOMPLETE
                ),
            ),
        )
        return quality, metrics

    def extract_performance_metrics(
        self, run_id: UUID, start_time: float
    ) -> tuple[PerformanceMeasurement, tuple[PerformanceMetric, ...]]:
        if self._connection is None:
            raise RuntimeError(
                "MetricEngine not connected. Call connect() first or use as context manager."
            )
        end_time = time.monotonic()
        latency_ms = (end_time - start_time) * 1000
        telemetry = self._read_telemetry(run_id)
        strict = self.config.strict if self.config else False
        strict_token_unavailable = strict and telemetry["token_source"] == "unavailable"
        strict_embedding_unavailable = strict and (
            telemetry.get("embedding_batch_count", 0) == 0
            or telemetry["embedding_elapsed_seconds"] <= 0
        )
        strict_cpu_unavailable = strict and (
            telemetry["cpu_samples"] == 0 or not telemetry.get("telemetry_tables_exist")
        )
        strict_gpu_unavailable = strict and (
            telemetry["gpu_samples"] == 0 or not telemetry.get("telemetry_tables_exist")
        )
        with self._connection.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM semantic_calls WHERE run_id = %s", (run_id,)
            )
            semantic_row = cur.fetchone()
        semantic_calls = int(semantic_row[0] or 0) if semantic_row else 0
        token_completeness = self._check_token_completeness(run_id)
        embedding_completeness = self._check_embedding_completeness(run_id)
        total_tokens = telemetry["total_tokens"]
        token_source = telemetry["token_source"]
        if strict_token_unavailable:
            token_source = "unavailable"
            total_tokens = None
        token_method = (
            "endpoint"
            if token_source == "endpoint"
            else ("tokenizer" if token_source == "tokenizer" else "estimated")
        )
        if token_source == "not_invoked":
            token_formula = (
                "not_invoked — deterministic fixture did not execute a model; "
                "token usage is intentionally NOT_APPLICABLE"
            )
        else:
            token_formula = (
                f"SUM(endpoint_usage_records.total_tokens) — source={token_source}"
                if token_source != "unavailable"
                else "unavailable — endpoint_usage_records has no measured token data"
            )
        if token_source not in ("unavailable", "not_invoked"):
            uncovered = token_completeness.get("uncovered_calls", 0)
            if uncovered > 0:
                token_formula = (
                    f"SUM(endpoint_usage_records.total_tokens) — source={token_source};"
                    f" {semantic_calls} calls, {token_completeness.get('usage_records', 0)} records,"
                    f" {uncovered} uncovered → INCOMPLETE"
                )
        cache_lookups = telemetry["cache_lookups"]
        cache_hits = telemetry["cache_hits"]
        if cache_lookups > 0:
            cache_hit_rate = round(cache_hits / cache_lookups, 6)
            cache_formula = (
                f"run_cache_events: hits({cache_hits}) / lookups({cache_lookups})"
            )
        else:
            cache_hit_rate = None
            cache_formula = (
                "unavailable — no lookups in the versioned release cache-stage set"
            )
        cache_event_ids: tuple[str, ...] = ()
        cache_stages: tuple[str, ...] = ()
        try:
            cursor = self._connection.execute(
                """SELECT ARRAY_AGG(id::text) AS event_ids,
                          ARRAY_AGG(DISTINCT stage) AS stages
                   FROM run_cache_events
                   WHERE run_id = %s
                     AND event_type = 'lookup'
                     AND stage = ANY(%s)""",
                (str(run_id), list(RELEASE_CACHE_STAGES)),
            )
            row = cursor.fetchone()
            if row and isinstance(row[0], (list, tuple)):
                cache_event_ids = tuple(row[0])
            if row and isinstance(row[1], (list, tuple)):
                cache_stages = tuple(row[1])
        except Exception:  # noqa: BLE001
            try:
                self._connection.rollback()
            except Exception:  # noqa: S110, BLE001
                pass
        embedding_throughput = telemetry["embedding_throughput"]
        emb_failures: list[str] = []
        if embedding_completeness.get("failed_count", 0) > 0:
            emb_failures.append(
                f"failed_count={embedding_completeness['failed_count']}"
            )
        if embedding_completeness.get("text_vector_mismatch", False):
            emb_failures.append(
                f"total_texts({embedding_completeness['total_texts']}) != "
                f"vector_count({embedding_completeness['vector_count']})"
            )
        if (
            telemetry["embedding_batch_count"] > 0
            and telemetry["embedding_elapsed_seconds"] > 0
        ):
            emb_formula = (
                f"run_embedding_throughput: {telemetry['embedding_total_texts']}/"
                f"{telemetry['embedding_elapsed_seconds']:.3f}s"
            )
            if emb_failures:
                emb_formula += " — completeness: " + "; ".join(emb_failures)
        elif strict_embedding_unavailable:
            embedding_throughput = None
            emb_formula = (
                "unavailable — no completed embedding work with measured duration"
            )
        else:
            embedding_throughput = max(0.0, 1000.0 / max(1, latency_ms / 100))
            emb_formula = (
                "1000 / max(1, latency_ms / 100) — run_embedding_throughput absent"
            )
        cpu_pct = telemetry["cpu_mean_percent"]
        if cpu_pct is None:
            cpu_pct = None if strict_cpu_unavailable else self._legacy_cpu_percent()
        cpu_valid_samples = telemetry["cpu_samples"] > 0 and telemetry.get(
            "telemetry_tables_exist"
        )
        cpu_formula = (
            "periodic_mean(run_resource_samples.value); scope=current benchmark process; "
            "collector=psutil.Process.cpu_percent(interval=None)/logical_cpu_count; "
            f"samples={telemetry['cpu_samples']}"
            if cpu_valid_samples
            else (
                "unavailable — no measured process-scoped CPU samples in run window"
                if strict_cpu_unavailable
                else (
                    "psutil.cpu_percent(interval=0.1) — single sample"
                    if _HAS_PSUTIL
                    else "0.0 — psutil not available"
                )
            )
        )
        gpu_mem = telemetry["gpu_mean_memory_mb"]
        if gpu_mem is None:
            gpu_mem = None if strict_gpu_unavailable else self._legacy_gpu_memory()
        gpu_valid_samples = telemetry["gpu_samples"] > 0 and telemetry.get(
            "telemetry_tables_exist"
        )
        gpu_formula = (
            "periodic_mean(run_resource_samples.value); scope=explicit NVML device identity; "
            f"samples={telemetry['gpu_samples']}"
            if gpu_valid_samples
            else (
                "unavailable — no measured GPU samples in run window"
                if strict_gpu_unavailable
                else (
                    "pynvml.nvmlDeviceGetMemoryInfo — single sample"
                    if _HAS_PYNVML and gpu_mem is not None
                    else "0.0 — NVML not available"
                )
            )
        )
        performance = PerformanceMeasurement(
            schema_version="performance-measurement-v2",
            total_latency_ms=round(latency_ms, 2),
            total_tokens=total_tokens,
            semantic_calls=semantic_calls,
            cache_hit_rate=round(cache_hit_rate, 6)
            if cache_hit_rate is not None
            else None,
            cache_miss_rate=round(1.0 - cache_hit_rate, 6)
            if cache_hit_rate is not None
            else None,
            embedding_throughput=round(embedding_throughput, 3)
            if embedding_throughput is not None
            else None,
            gpu_memory_mb=gpu_mem,
            cpu_percent=round(cpu_pct, 2) if cpu_pct is not None else None,
        )
        token_status = (
            MetricStatus.NOT_APPLICABLE
            if token_source == "not_invoked"
            else MetricStatus.UNAVAILABLE
            if strict_token_unavailable
            else MetricStatus.INCOMPLETE
            if token_completeness.get("uncovered_calls", 0) > 0
            else MetricStatus.MEASURED
        )
        cache_status = (
            MetricStatus.UNAVAILABLE if cache_lookups == 0 else MetricStatus.MEASURED
        )
        emb_status = (
            MetricStatus.UNAVAILABLE
            if strict_embedding_unavailable
            else MetricStatus.INCOMPLETE
            if embedding_completeness.get("failed_count", 0) > 0
            or embedding_completeness.get("text_vector_mismatch", False)
            else MetricStatus.MEASURED
        )
        cpu_status = (
            MetricStatus.UNAVAILABLE
            if strict_cpu_unavailable
            else MetricStatus.MEASURED
            if telemetry["cpu_samples"] > 0
            else MetricStatus.UNAVAILABLE
        )
        gpu_status = (
            MetricStatus.UNAVAILABLE
            if strict_gpu_unavailable
            else MetricStatus.MEASURED
            if telemetry["gpu_samples"] > 0
            else MetricStatus.UNAVAILABLE
        )
        cpu_source = self._read_resource_source(run_id, "cpu")
        gpu_source = self._read_resource_source(run_id, "gpu")
        cpu_completeness = self._check_resource_completeness(run_id, "cpu")
        gpu_completeness = self._check_resource_completeness(run_id, "gpu")
        cpu_nonmeasured = (
            cpu_completeness["total_count"] - cpu_completeness["measured_count"]
        )
        gpu_nonmeasured = (
            gpu_completeness["total_count"] - gpu_completeness["measured_count"]
        )
        if cpu_completeness["invalid_count"]:
            cpu_status = MetricStatus.INVALID
        elif cpu_nonmeasured and cpu_completeness["measured_count"]:
            cpu_status = MetricStatus.INCOMPLETE
        elif (
            strict
            and cpu_status == MetricStatus.MEASURED
            and cpu_completeness["measured_count"] < STRICT_MIN_CPU_SAMPLES
        ):
            cpu_status = MetricStatus.INCOMPLETE
            cpu_formula += (
                "; incomplete — strict periodic window requires at least "
                f"{STRICT_MIN_CPU_SAMPLES} measured CPU samples, observed "
                f"{cpu_completeness['measured_count']}"
            )
        if gpu_completeness["invalid_count"]:
            gpu_status = MetricStatus.INVALID
        elif gpu_nonmeasured and gpu_completeness["measured_count"]:
            gpu_status = MetricStatus.INCOMPLETE
        elif (
            strict
            and gpu_status == MetricStatus.MEASURED
            and gpu_completeness["measured_count"] < STRICT_MIN_GPU_SAMPLES
        ):
            gpu_status = MetricStatus.INCOMPLETE
            gpu_formula += (
                "; incomplete — strict periodic window requires at least "
                f"{STRICT_MIN_GPU_SAMPLES} measured GPU samples, observed "
                f"{gpu_completeness['measured_count']}"
            )
        if gpu_status == MetricStatus.MEASURED and not gpu_source["device_uuid"]:
            gpu_status = MetricStatus.INCOMPLETE
        if cpu_status == MetricStatus.MEASURED and cpu_completeness["missing_window"]:
            cpu_status = MetricStatus.INCOMPLETE
        if gpu_status == MetricStatus.MEASURED and gpu_completeness["missing_window"]:
            gpu_status = MetricStatus.INCOMPLETE
        if cpu_status != MetricStatus.MEASURED or gpu_status != MetricStatus.MEASURED:
            performance = PerformanceMeasurement(
                schema_version=performance.schema_version,
                total_latency_ms=performance.total_latency_ms,
                total_tokens=performance.total_tokens,
                semantic_calls=performance.semantic_calls,
                cache_hit_rate=performance.cache_hit_rate,
                cache_miss_rate=performance.cache_miss_rate,
                embedding_throughput=performance.embedding_throughput,
                cpu_percent=(
                    performance.cpu_percent
                    if cpu_status == MetricStatus.MEASURED
                    else None
                ),
                gpu_memory_mb=(
                    performance.gpu_memory_mb
                    if gpu_status == MetricStatus.MEASURED
                    else None
                ),
            )
        metrics = (
            PerformanceMetric(
                name="total_latency_ms",
                value=performance.total_latency_ms,
                source=MetricSource(
                    table="benchmark_harness",
                    column="monotonic_end - monotonic_start",
                    run_id=str(run_id),
                    method="duration",
                ),
                formula="wall_clock_ms(monotonic_start, monotonic_end)",
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
                formula="COUNT(*) FROM semantic_calls WHERE run_id = %s",
                status=MetricStatus.MEASURED,
            ),
            PerformanceMetric(
                name="total_tokens",
                value=float(performance.total_tokens)
                if performance.total_tokens is not None
                else None,
                source=MetricSource(
                    table="endpoint_usage_records"
                    if token_source != "unavailable"
                    else "run_performance_telemetry",
                    column="total_tokens"
                    if token_source != "unavailable"
                    else "total_tokens (fallback)",
                    run_id=str(run_id),
                    method=token_method,
                ),
                formula=token_formula,
                status=token_status,
            ),
            PerformanceMetric(
                name="cache_hit_rate",
                value=performance.cache_hit_rate,
                source=MetricSource(
                    table="run_cache_events",
                    column="event_type, hit",
                    run_id=str(run_id),
                    method="ratio",
                    event_ids=cache_event_ids,
                    stages=cache_stages,
                    stage_set_version=RELEASE_CACHE_STAGE_SET_VERSION,
                ),
                formula=cache_formula,
                status=cache_status,
            ),
            PerformanceMetric(
                name="embedding_throughput",
                value=performance.embedding_throughput,
                source=MetricSource(
                    table="run_embedding_throughput",
                    column="total_texts, elapsed_seconds",
                    run_id=str(run_id),
                    method="ratio",
                ),
                formula=emb_formula,
                status=emb_status,
            ),
            PerformanceMetric(
                name="cpu_percent",
                value=performance.cpu_percent,
                source=MetricSource(
                    table="run_resource_samples",
                    column="AVG(value) FILTER (status = 'measured')",
                    run_id=str(run_id),
                    method="periodic_run_window_mean",
                    event_ids=cpu_source["record_ids"],
                    sample_count=cpu_source["measured_count"],
                    device_type="cpu",
                    device_index=cpu_source["device_index"],
                    collector=cpu_source["collector"],
                    collector_version=cpu_source["collector_version"],
                    status_counts=cpu_source["status_counts"],
                ),
                formula=cpu_formula,
                status=cpu_status,
            ),
            PerformanceMetric(
                name="gpu_memory_mb",
                value=performance.gpu_memory_mb,
                source=MetricSource(
                    table="run_resource_samples",
                    column="AVG(value) FILTER (status = 'measured')",
                    run_id=str(run_id),
                    method="periodic_run_window_mean",
                    event_ids=gpu_source["record_ids"],
                    sample_count=gpu_source["measured_count"],
                    device_type="gpu",
                    device_index=gpu_source["device_index"],
                    device_uuid=gpu_source["device_uuid"],
                    collector=gpu_source["collector"],
                    collector_version=gpu_source["collector_version"],
                    status_counts=gpu_source["status_counts"],
                ),
                formula=gpu_formula,
                status=gpu_status,
            ),
        )
        return performance, metrics

    def _check_token_completeness(self, run_id: UUID) -> dict[str, Any]:
        result: dict[str, Any] = {
            "semantic_calls": 0,
            "usage_records": 0,
            "uncovered_calls": 0,
        }
        try:
            cur = self._connection.execute(
                """SELECT
                       (SELECT COUNT(*) FROM semantic_calls WHERE run_id = %s),
                       (SELECT COUNT(*) FROM endpoint_usage_records WHERE run_id = %s)""",
                (str(run_id), str(run_id)),
            )
            row = cur.fetchone()
            if row and len(row) >= 2:
                sc = int(row[0] or 0)
                ur = int(row[1] or 0)
                result.update(
                    semantic_calls=sc,
                    usage_records=ur,
                    uncovered_calls=max(0, sc - ur),
                )
        except Exception:  # noqa: BLE001
            try:
                self._connection.rollback()
            except Exception:  # noqa: S110, BLE001
                pass
        return result

    def _check_embedding_completeness(self, run_id: UUID) -> dict[str, Any]:
        result: dict[str, Any] = {
            "batch_count": 0,
            "vector_count": 0,
            "failed_count": 0,
            "total_texts": 0,
            "elapsed_seconds": 0.0,
            "text_vector_mismatch": False,
        }
        try:
            cur = self._connection.execute(
                """SELECT COALESCE(SUM(batch_count),0),
                          COALESCE(SUM(vector_count),0),
                          COALESCE(SUM(failed_count),0),
                          COALESCE(SUM(total_texts),0),
                          COALESCE(SUM(elapsed_seconds),0.0)
                     FROM run_embedding_throughput WHERE run_id = %s""",
                (str(run_id),),
            )
            row = cur.fetchone()
            if row and len(row) >= 5:
                batch_count = int(row[0] or 0)
                vector_count = int(row[1] or 0)
                failed_count = int(row[2] or 0)
                total_texts = int(row[3] or 0)
                elapsed = float(row[4] or 0.0)
                result.update(
                    batch_count=batch_count,
                    vector_count=vector_count,
                    failed_count=failed_count,
                    total_texts=total_texts,
                    elapsed_seconds=elapsed,
                    text_vector_mismatch=(total_texts != vector_count),
                )
        except Exception:  # noqa: BLE001
            try:
                self._connection.rollback()
            except Exception:  # noqa: S110, BLE001
                pass
        return result

    def _check_resource_completeness(
        self, run_id: UUID, device_type: str
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "total_count": 0,
            "measured_count": 0,
            "unavailable_count": 0,
            "invalid_count": 0,
            "partial_count": 0,
            "stale_count": 0,
            "missing_window": 0,
            "has_failure_reason": 0,
        }
        try:
            cur = self._connection.execute(
                """SELECT COUNT(*),
                          COUNT(*) FILTER (WHERE status='measured'),
                          COUNT(*) FILTER (WHERE status='unavailable'),
                          COUNT(*) FILTER (WHERE status='invalid'),
                          COUNT(*) FILTER (WHERE status='partial'),
                          COUNT(*) FILTER (WHERE status='stale'),
                          COUNT(*) FILTER (WHERE window_start IS NULL OR window_end IS NULL),
                          COUNT(*) FILTER (WHERE failure_reason IS NOT NULL AND failure_reason != '')
                     FROM run_resource_samples
                    WHERE run_id=%s AND device_type=%s""",
                (str(run_id), device_type),
            )
            row = cur.fetchone()
            if row and len(row) >= 8:
                result.update(
                    total_count=int(row[0] or 0),
                    measured_count=int(row[1] or 0),
                    unavailable_count=int(row[2] or 0),
                    invalid_count=int(row[3] or 0),
                    partial_count=int(row[4] or 0),
                    stale_count=int(row[5] or 0),
                    missing_window=int(row[6] or 0),
                    has_failure_reason=int(row[7] or 0),
                )
        except Exception:  # noqa: BLE001
            try:
                self._connection.rollback()
            except Exception:  # noqa: S110, BLE001
                pass
        return result

    def _read_resource_source(self, run_id: UUID, device_type: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "record_ids": (),
            "measured_count": 0,
            "total_count": 0,
            "invalid_count": 0,
            "device_index": None,
            "device_uuid": "",
            "collector": "",
            "collector_version": "",
            "status_counts": (),
        }
        try:
            cur = self._connection.execute(
                """SELECT id::text, device_index, COALESCE(device_uuid, ''),
                          collector, collector_version, status
                     FROM run_resource_samples
                    WHERE run_id=%s AND device_type=%s
                    ORDER BY sample_number,id""",
                (str(run_id), device_type),
            )
            rows = cur.fetchall()
        except Exception:  # noqa: BLE001
            try:
                self._connection.rollback()
            except Exception:  # noqa: BLE001, S110
                pass
            return result
        if not isinstance(rows, (list, tuple)):
            return result
        counts: dict[str, int] = {}
        for row in rows:
            counts[row[5]] = counts.get(row[5], 0) + 1
        if rows:
            first = next((row for row in rows if row[5] == "measured"), rows[0])
            result.update(
                record_ids=tuple(row[0] for row in rows),
                measured_count=counts.get("measured", 0),
                total_count=len(rows),
                invalid_count=counts.get("invalid", 0),
                device_index=first[1],
                device_uuid=first[2],
                collector=first[3],
                collector_version=first[4],
                status_counts=tuple(sorted(counts.items())),
            )
        return result

    def _read_telemetry(self, run_id: UUID) -> dict[str, Any]:
        result: dict[str, Any] = {
            "total_tokens": 0,
            "token_source": "unavailable",
            "cache_lookups": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "embedding_batch_count": 0,
            "embedding_throughput": 0.0,
            "embedding_total_texts": 0,
            "embedding_elapsed_seconds": 0.0,
            "cpu_samples": 0,
            "cpu_mean_percent": None,
            "gpu_samples": 0,
            "gpu_mean_memory_mb": None,
            "telemetry_tables_exist": False,
        }
        try:
            cur = self._connection.execute(
                """SELECT total_tokens, token_source,
                          cache_lookups, cache_hits, cache_misses,
                          embedding_batch_count, embedding_throughput,
                          embedding_vector_count, embedding_elapsed_seconds,
                          cpu_mean_percent, cpu_samples,
                          gpu_mean_memory_mb, gpu_samples
                     FROM run_performance_telemetry WHERE run_id=%s""",
                (str(run_id),),
            )
            row = cur.fetchone()
            result["telemetry_tables_exist"] = True
            if row and row[0] is not None:
                result.update(
                    total_tokens=row[0] or 0,
                    token_source=row[1] or "unavailable",
                    cache_lookups=row[2] or 0,
                    cache_hits=row[3] or 0,
                    cache_misses=row[4] or 0,
                    embedding_batch_count=row[5] or 0,
                    embedding_throughput=row[6] or 0.0,
                    embedding_total_texts=row[7] or 0,
                    embedding_elapsed_seconds=row[8] or 0.0,
                    cpu_samples=row[10] or 0,
                    gpu_samples=row[12] or 0,
                )
                if row[9] is not None and (row[10] or 0) > 0:
                    result["cpu_mean_percent"] = round(float(row[9]), 2)
                if row[11] is not None and (row[12] or 0) > 0:
                    result["gpu_mean_memory_mb"] = round(float(row[11]), 2)
        except Exception as exc:  # noqa: BLE001
            try:
                self._connection.rollback()
            except Exception:  # noqa: S110, BLE001
                pass
            logger.warning(
                "run_performance_telemetry query failed — falling back to legacy metric extraction: %s",
                exc,
            )
        return result

    def _legacy_cpu_percent(self) -> float:
        if _HAS_PSUTIL:
            try:
                return round(psutil.cpu_percent(interval=0.1), 2)
            except Exception:  # noqa: BLE001
                return 0.0
        return 0.0

    def _legacy_gpu_memory(self) -> float | None:
        if not _HAS_PYNVML:
            return None
        try:
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            result = round(info.used / (1024 * 1024), 2)
            pynvml.nvmlShutdown()
            return result
        except Exception:  # noqa: BLE001
            try:
                pynvml.nvmlShutdown()
            except Exception:  # noqa: S110, BLE001
                pass
            return None


@dataclass(frozen=True)
class CampaignRun:
    schema_version: str = "campaign-run-v1"
    campaign_id: str = ""
    run_id: str = ""
    mode: str = ""
    objective_id: str = ""
    quality: QualityMeasurement | None = None
    performance: PerformanceMeasurement | None = None
    quality_metrics: tuple[QualityMetric, ...] = ()
    performance_metrics: tuple[PerformanceMetric, ...] = ()
    integrity_checks: tuple[DeterministicIntegrityCheck, ...] = ()
    orchestration_outcome: str | None = None
    errors: tuple[str, ...] = ()
    duration_ms: float = 0.0


@dataclass(frozen=True)
class ReproducibilityComparison:
    schema_version: str = "reproducibility-comparison-v1"
    run_a_id: str = ""
    run_b_id: str = ""
    mode: str = ""
    objective_id: str = ""
    quality_tolerances: tuple[tuple[str, float, float, float], ...] = ()
    performance_tolerances: tuple[tuple[str, float, float, float], ...] = ()
    policy_version: str = REPRODUCIBILITY_POLICY_VERSION
    relative_tolerance: float = 0.15
    operational_ratio_limit: float = 2.0
    operational_absolute_tolerances: tuple[tuple[str, float], ...] = ()
    all_within_tolerance: bool = True
    observations: tuple[str, ...] = ()
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReleaseBenchmarkResult:
    schema_version: str = "release-benchmark-result-v1"
    campaign_id: str = ""
    campaign_timestamp: str = ""
    environment: dict[str, Any] = field(default_factory=dict)
    runs: tuple[CampaignRun, ...] = ()
    comparison: WorkflowComparison | None = None
    reproducibility: ReproducibilityComparison | None = None
    recommendation: ReleaseRecommendation | None = None
    total_duration_ms: float = 0.0

    def summary(self) -> str:
        lines = [
            f"Release Benchmark — Campaign {self.campaign_id}",
            f"Timestamp: {self.campaign_timestamp}",
            f"Duration: {self.total_duration_ms:.0f}ms",
            f"Runs: {len(self.runs)}",
        ]
        if self.recommendation:
            lines.append(f"Recommendation: {self.recommendation.outcome}")
        if self.reproducibility:
            lines.append(
                f"Reproducibility: {'PASS' if self.reproducibility.all_within_tolerance else 'FAIL'}"
            )
        return "\n".join(lines)


class ReleaseBenchmarkRunner:
    def __init__(
        self,
        dataset: BenchmarkDataset | BenchmarkDatasetLoader,
        config: ReleaseBenchmarkConfig | None = None,
    ) -> None:
        self.loader = (
            dataset
            if isinstance(dataset, BenchmarkDatasetLoader)
            else BenchmarkDatasetLoader(dataset)
        )
        self.config = config or ReleaseBenchmarkConfig()
        self.integrity_checker = DeterministicIntegrityChecker(
            blob_root=self.config.blob_root,
        )

    def _validate_modes(self) -> None:
        if LEGACY_MODE_FORBIDDEN and "legacy" in self.config.execution_modes:
            raise RuntimeError(
                "Benchmark mode 'legacy' is forbidden — no distinct retained baseline exists. "
                f"Use one of: {', '.join(RELEASE_MODES)}"
            )
        seen: set[str] = set()
        for mode in self.config.execution_modes:
            if mode in seen:
                raise RuntimeError(f"Duplicate benchmark mode: {mode}")
            if mode not in RELEASE_MODES:
                raise RuntimeError(
                    f"Unknown benchmark mode: {mode}. Valid modes: {', '.join(RELEASE_MODES)}"
                )
            seen.add(mode)
        if (
            "agent_led" in self.config.execution_modes
            and self.config.host_artifact_supplier is None
        ):
            raise RuntimeError(
                "agent_led benchmark requires a HostArtifactSupplier to ensure genuine external authority"
            )

    def _select_objectives(self) -> list[BenchmarkObjective]:
        if self.config.objective_ids:
            return [
                obj
                for obj in self.loader.objectives
                if obj.id in self.config.objective_ids
            ]
        return list(self.loader.objectives)

    def run(self) -> ReleaseBenchmarkResult:
        start = time.monotonic()
        campaign_id = f"fr_bench_{uuid4().hex[:8]}"
        campaign_timestamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        self._validate_modes()
        objectives = self._select_objectives()
        operational_limit = self.loader.quality_thresholds.get(
            "max_operational_reproducibility_ratio",
            self.config.operational_reproducibility_ratio_limit,
        )
        environment: dict[str, Any] = {
            "python_version": os.sys.version.split()[0],
            "platform": os.uname().sysname + " " + os.uname().release,
            "database_url_set": bool(self.config.database_url),
            "blob_root_set": bool(self.config.blob_root),
            "dataset_version": self.loader.dataset.version,
            "modes": ",".join(self.config.execution_modes),
            "reproducibility_policy_version": REPRODUCIBILITY_POLICY_VERSION,
            "reproducibility_relative_tolerance": self.config.reproducibility_tolerance,
            "operational_reproducibility_ratio_limit": float(operational_limit),
            "operational_absolute_tolerances": dict(OPERATIONAL_ABSOLUTE_TOLERANCES),
        }
        runs: list[CampaignRun] = []
        metric_engine: MetricEngine | None = None
        if self.config.database_url:
            metric_engine = MetricEngine(self.config.database_url, config=self.config)
            try:
                metric_engine.connect()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "MetricEngine failed to connect; campaign runs will record errors"
                )
                metric_engine = None
        try:
            for mode in self.config.execution_modes:
                for objective in objectives:
                    runs.append(
                        self._execute_benchmark_run(
                            campaign_id, mode, objective, metric_engine
                        )
                    )
        finally:
            if metric_engine is not None:
                metric_engine.close()
        workflow_results = self._campaign_to_workflow_results(runs)
        comparison = self._build_comparison(workflow_results)
        recommendation = self._build_recommendation(comparison)
        return ReleaseBenchmarkResult(
            schema_version="release-benchmark-result-v1",
            campaign_id=campaign_id,
            campaign_timestamp=campaign_timestamp,
            environment=environment,
            runs=tuple(runs),
            comparison=comparison,
            recommendation=recommendation,
            total_duration_ms=(time.monotonic() - start) * 1000,
        )

    def _execute_benchmark_run(
        self,
        campaign_id: str,
        mode: str,
        objective: BenchmarkObjective,
        metric_engine: MetricEngine | None,
    ) -> CampaignRun:
        start = time.monotonic()
        errors: list[str] = []
        run_id = ""
        quality: QualityMeasurement | None = None
        performance: PerformanceMeasurement | None = None
        quality_metrics: tuple[QualityMetric, ...] = ()
        performance_metrics: tuple[PerformanceMetric, ...] = ()
        integrity_checks: tuple[DeterministicIntegrityCheck, ...] = ()
        resource_samples: tuple[Any, ...] = ()
        orchestration_outcome: str | None = None
        try:
            from research_store.config import StoreConfig
            from research_store.container import build_orchestrator, build_run_service
            from research_store.orchestrator import OrchestratorConfig

            config = StoreConfig.from_env()
            if self.config.database_url:
                config = replace(config, database_url=self.config.database_url)
            config.require_database()
            os.environ.pop("FIRECRAWL_RELEASE_DETERMINISTIC_FIXTURES", None)
            if mode == "deterministic_debug":
                os.environ["FIRECRAWL_RELEASE_DETERMINISTIC_FIXTURES"] = "1"
            orchestrator = build_orchestrator(
                config,
                orchestrator_config=OrchestratorConfig(
                    execution_mode=mode,
                    max_adaptive_cycles=10,
                    host_artifact_supplier=self.config.host_artifact_supplier,
                ),
            )
            run_service = build_run_service(config)
            external_id = f"fr_bench_{mode}_{objective.id}_{uuid4().hex[:8]}"
            run_status = run_service.create(
                objective=objective.objective,
                external_id=external_id,
                execution_mode=mode,
            )
            run_id = str(run_status.id)
            from budget_policy import conservative_research_spec
            from research_domain import serialize_model

            spec = serialize_model(
                conservative_research_spec(objective.objective, "general")
            )
            candidate_sha = os.environ.get("CANDIDATE_SHA")
            if not candidate_sha:
                raise ValueError(
                    "CANDIDATE_SHA environment variable is required for strict benchmark campaigns"
                )
            configured_queries = tuple(
                f"https://raw.githubusercontent.com/fvanevski/firecrawl_skill/{candidate_sha}/{source.file_path}"
                for source in objective.known_relevant_sources[:3]
            )
            search_plan = {
                "schema_version": "search-plan-v1",
                "research_spec_id": spec["research_spec_id"],
                "revision": 1,
                "queries": [
                    {
                        "query_id": str(uuid4()),
                        "query": query,
                        "facet": "benchmark_source",
                        "target_question_ids": [
                            q["question_id"] for q in spec["questions"]
                        ],
                        "target_claim_ids": [],
                        "intended_source_classes": list(
                            objective.expected_source_classes
                        ),
                        "expected_organizations": [],
                        "freshness_requirement": spec["time_window"],
                        "expected_contribution": "answer",
                        "domain_restrictions": [],
                        "negative_terms": [],
                        "priority": 1,
                    }
                    for query in configured_queries
                ],
            }
            from research_store.resource_sampler import ResourceSampler

            sampler = ResourceSampler(interval_seconds=1.0)
            sampler.start_periodic_window()
            try:
                orchestration_result = orchestrator.run(
                    run_id=run_status.id, spec=spec, search_plan=search_plan
                )
                orchestration_outcome = orchestration_result.outcome
                if orchestration_result.outcome != "completed":
                    errors.append(
                        "orchestration did not complete: "
                        f"outcome={orchestration_result.outcome}; "
                        f"error={orchestration_result.error or 'none'}"
                    )
            finally:
                cpu_samples, gpu_samples = sampler.stop_periodic_window()
                resource_samples = tuple(cpu_samples + gpu_samples)
            if metric_engine is not None and run_id:
                from research_store.telemetry_service import PerformanceTelemetryService

                connection = metric_engine._connection
                telemetry_svc = PerformanceTelemetryService(connection)
                self._populate_endpoint_usage(telemetry_svc, UUID(run_id), connection)
                self._populate_cache_events(telemetry_svc, UUID(run_id), connection)
                self._persist_resource_samples(
                    telemetry_svc, UUID(run_id), resource_samples
                )
                telemetry_svc.build_summary(UUID(run_id), stages=RELEASE_CACHE_STAGES)
                connection.commit()
            if metric_engine is not None:
                quality, quality_metrics = metric_engine.extract_quality_metrics(
                    UUID(run_id), objective=objective
                )
                performance, performance_metrics = (
                    metric_engine.extract_performance_metrics(UUID(run_id), start)
                )
            else:
                errors.append("no metric engine available — metrics not extracted")
        except Exception as exc:
            logger.exception(
                "benchmark execution FAILED for mode=%s objective=%s",
                mode,
                objective.id,
            )
            errors.append(f"execution failed: {exc}")
        try:
            integrity_checks = tuple(
                self.integrity_checker.check(name)
                for name in self.config.integrity_checks
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "integrity checks FAILED for mode=%s objective=%s",
                mode,
                objective.id,
            )
            errors.append("integrity checks failed")
        return CampaignRun(
            campaign_id=campaign_id,
            run_id=run_id,
            mode=mode,
            objective_id=objective.id,
            quality=quality,
            performance=performance,
            quality_metrics=quality_metrics,
            performance_metrics=performance_metrics,
            integrity_checks=integrity_checks,
            orchestration_outcome=orchestration_outcome,
            errors=tuple(errors),
            duration_ms=(time.monotonic() - start) * 1000,
        )

    def _populate_endpoint_usage(self, telemetry_svc, run_id: UUID, connection) -> None:
        from research_store.telemetry_service import EndpointUsageRecord
        from research_store.token_accounting import extract_endpoint_usage

        with connection.cursor() as cur:
            cur.execute(
                "SELECT id,response_metadata FROM semantic_calls WHERE run_id=%s AND status='complete'",
                (str(run_id),),
            )
            for call_id, response_metadata in cur.fetchall():
                accounting = extract_endpoint_usage(response_metadata or {})
                if accounting.source == "unavailable":
                    continue
                telemetry_svc.record_endpoint_usage(
                    EndpointUsageRecord(
                        run_id=str(run_id),
                        call_id=str(call_id),
                        endpoint_type="generative",
                        provider="openai-compatible",
                        model="",
                        model_revision="",
                        prompt_tokens=accounting.prompt_tokens or 0,
                        completion_tokens=accounting.completion_tokens or 0,
                        total_tokens=accounting.total_tokens or 0,
                        source=accounting.source,
                    )
                )

    def _persist_resource_samples(
        self, telemetry_svc, run_id: UUID, samples: tuple[Any, ...]
    ) -> None:
        for sample in samples:
            bound = sample.__class__(**{**sample.__dict__, "run_id": str(run_id)})
            telemetry_svc.record_resource_sample(bound)

    def _populate_cache_events(self, telemetry_svc, run_id: UUID, connection) -> None:
        with connection.cursor() as cur:
            cur.execute(
                """SELECT stage_name, artifact->>'cache_hit', semantic_call_id, model_name
                     FROM synthesis_stages
                    WHERE run_id=%s AND stage_name=ANY(%s) AND artifact ? 'cache_hit'""",
                (str(run_id), list(RELEASE_CACHE_STAGES)),
            )
            for stage, raw_hit, call_id, model_name in cur.fetchall():
                telemetry_svc.record_cache_event(
                    run_id,
                    stage,
                    "lookup",
                    key_hash=str(call_id or ""),
                    model_fingerprint=str(model_name or ""),
                    hit=(raw_hit == "true"),
                )

    def _campaign_to_workflow_results(
        self, runs: list[CampaignRun]
    ) -> list[WorkflowRunResult]:
        results: list[WorkflowRunResult] = []
        for run in runs:
            quality = run.quality or QualityMeasurement(
                schema_version="quality-measurement-v3",
                candidate_recall=None,
                source_quality_score=None,
                coverage_completeness=None,
                unsupported_claim_rate=None,
                citation_accuracy=None,
                report_quality_score=None,
            )
            performance = run.performance or PerformanceMeasurement(
                schema_version="performance-measurement-v2",
                total_latency_ms=0.0,
                total_tokens=None,
                semantic_calls=0,
                cache_hit_rate=None,
                cache_miss_rate=None,
                embedding_throughput=None,
                gpu_memory_mb=None,
                cpu_percent=None,
            )
            results.append(
                WorkflowRunResult(
                    schema_version="workflow-run-result-v1",
                    workflow_mode=run.mode,
                    quality=quality,
                    performance=performance,
                    integrity_checks=run.integrity_checks,
                    run_id=UUID(run.run_id) if run.run_id else None,
                    errors=run.errors,
                    quality_metrics=run.quality_metrics,
                    performance_metrics=run.performance_metrics,
                )
            )
        return results

    @staticmethod
    def _mean(values: list[float | int | None]) -> float | None:
        if any(value is None for value in values):
            return None
        concrete = [float(value) for value in values if value is not None]
        return sum(concrete) / len(concrete) if concrete else None

    def _build_comparison(self, results: list[WorkflowRunResult]) -> WorkflowComparison:
        if not results:
            raise ValueError("No campaign results to compare")
        if len({r.workflow_mode for r in results}) < 2:
            raise ValueError("At least 2 workflow modes required for comparison")
        mode_results: dict[str, list[WorkflowRunResult]] = {}
        for result in results:
            mode_results.setdefault(result.workflow_mode, []).append(result)

        def avg_quality(items: list[WorkflowRunResult]) -> QualityMeasurement | None:
            if not items:
                return None
            return QualityMeasurement(
                schema_version="quality-measurement-v3",
                candidate_recall=self._mean(
                    [r.quality.candidate_recall for r in items]
                ),
                source_quality_score=self._mean(
                    [r.quality.source_quality_score for r in items]
                ),
                coverage_completeness=self._mean(
                    [r.quality.coverage_completeness for r in items]
                ),
                unsupported_claim_rate=self._mean(
                    [r.quality.unsupported_claim_rate for r in items]
                ),
                citation_accuracy=self._mean(
                    [r.quality.citation_accuracy for r in items]
                ),
                report_quality_score=self._mean(
                    [r.quality.report_quality_score for r in items]
                ),
            )

        def avg_performance(
            items: list[WorkflowRunResult],
        ) -> PerformanceMeasurement | None:
            if not items:
                return None
            total_tokens = self._mean([r.performance.total_tokens for r in items])
            return PerformanceMeasurement(
                schema_version="performance-measurement-v2",
                total_latency_ms=(
                    self._mean([r.performance.total_latency_ms for r in items]) or 0.0
                ),
                total_tokens=int(total_tokens) if total_tokens is not None else None,
                semantic_calls=int(
                    self._mean([r.performance.semantic_calls for r in items]) or 0
                ),
                cache_hit_rate=self._mean(
                    [r.performance.cache_hit_rate for r in items]
                ),
                cache_miss_rate=self._mean(
                    [r.performance.cache_miss_rate for r in items]
                ),
                embedding_throughput=self._mean(
                    [r.performance.embedding_throughput for r in items]
                ),
                gpu_memory_mb=self._mean([r.performance.gpu_memory_mb for r in items]),
                cpu_percent=self._mean([r.performance.cpu_percent for r in items]),
            )

        first_mode = next(iter(mode_results))
        baseline_quality = avg_quality(mode_results[first_mode])
        baseline_perf = avg_performance(mode_results[first_mode])
        quality_vs_baseline: dict[str, float] = {}
        performance_vs_baseline: dict[str, float] = {}
        for mode, items in mode_results.items():
            if mode == first_mode:
                continue
            avg_q = avg_quality(items)
            avg_p = avg_performance(items)
            if (
                baseline_quality
                and avg_q
                and baseline_quality.candidate_recall is not None
                and avg_q.candidate_recall is not None
                and baseline_quality.candidate_recall > 0
            ):
                quality_vs_baseline[mode] = (
                    avg_q.candidate_recall / baseline_quality.candidate_recall
                )
            else:
                quality_vs_baseline[mode] = 1.0
            if baseline_perf and avg_p and baseline_perf.total_latency_ms > 0:
                performance_vs_baseline[mode] = (
                    avg_p.total_latency_ms / baseline_perf.total_latency_ms
                )
            else:
                performance_vs_baseline[mode] = 1.0
        return WorkflowComparison(
            schema_version="workflow-comparison-v1",
            dataset_version=self.loader.dataset.version,
            results=tuple(results),
            quality_vs_baseline=quality_vs_baseline,
            performance_vs_baseline=performance_vs_baseline,
            integrity_regression=any(
                not check.passed
                for result in results
                for check in result.integrity_checks
            ),
        )

    @staticmethod
    def _quality_statuses(result: WorkflowRunResult) -> dict[str, MetricStatus]:
        return {
            metric.name: metric.status
            for metric in result.quality_metrics
            if isinstance(metric, QualityMetric)
        }

    @staticmethod
    def _performance_statuses(result: WorkflowRunResult) -> dict[str, MetricStatus]:
        return {
            metric.name: metric.status
            for metric in result.performance_metrics
            if isinstance(metric, PerformanceMetric)
        }

    def _build_recommendation(
        self, comparison: WorkflowComparison
    ) -> ReleaseRecommendation:
        withdrawn: list[str] = []
        limitations = (
            list(self.config.known_limitations)
            if self.config.known_limitations
            else [
                "CPU-based embedding and reranking causes high latency (~8.5s per embedding batch)",
                "GPU is reserved for local LLM agents; embedding/reranker run on CPU",
                "Local embedding models (nomic-embed-text, bge-m3) may have lower recall than OpenAI",
                "Local reranker (cross-encoder) may be slower than cloud alternatives",
            ]
        )
        thresholds = self.loader.quality_thresholds
        mode_results: dict[str, list[WorkflowRunResult]] = {}
        for result in comparison.results:
            mode_results.setdefault(result.workflow_mode, []).append(result)
        quality_thresholds = (
            ("candidate_recall", "min_candidate_recall", 0.5, "min"),
            ("source_quality_score", "min_source_quality_score", 0.7, "min"),
            ("coverage_completeness", "min_coverage_completeness", 0.5, "min"),
            ("unsupported_claim_rate", "max_unsupported_claim_rate", 0.15, "max"),
            ("citation_accuracy", "min_citation_accuracy", 0.8, "min"),
        )
        for mode, items in mode_results.items():
            for result in items:
                statuses = self._quality_statuses(result)
                for (
                    field_name,
                    threshold_name,
                    default,
                    direction,
                ) in quality_thresholds:
                    value = getattr(result.quality, field_name)
                    if statuses.get(field_name) != MetricStatus.MEASURED:
                        value = None
                    threshold = float(thresholds.get(threshold_name, default))
                    if value is None:
                        continue
                    failed = (
                        value < threshold if direction == "min" else value > threshold
                    )
                    if failed:
                        operator = ">=" if direction == "min" else "<="
                        withdrawn.append(
                            f"{field_name} {operator} {threshold} — "
                            f"{mode} achieved {value:.3f}"
                        )
                if self.config.strict:
                    if result.errors:
                        withdrawn.append(
                            f"{mode} encountered execution errors: {result.errors}"
                        )
                    orchestration_outcome = getattr(
                        result, "orchestration_outcome", "completed"
                    )
                    if orchestration_outcome != "completed":
                        withdrawn.append(
                            f"{mode} orchestration did not complete "
                            f"(outcome: {orchestration_outcome})"
                        )
                    observed_quality = self._quality_statuses(result)
                    for metric in MANDATORY_QUALITY_METRICS:
                        status = observed_quality.get(metric, MetricStatus.UNAVAILABLE)
                        if status not in STRICT_ACCEPTABLE_QUALITY_STATUSES:
                            withdrawn.append(
                                f"quality metric {metric} is {status.value} (not measured) — "
                                f"{mode} cannot satisfy release policy"
                            )
                    observed_perf = self._performance_statuses(result)
                    for metric in MANDATORY_PERFORMANCE_METRICS:
                        status = observed_perf.get(metric, MetricStatus.UNAVAILABLE)
                        if status not in STRICT_ACCEPTABLE_PERFORMANCE_STATUSES:
                            withdrawn.append(
                                f"performance metric {metric} is {status.value} (not measured) — "
                                f"{mode} cannot satisfy release policy"
                            )
                    for check in result.integrity_checks:
                        if not check.passed:
                            withdrawn.append(
                                f"{mode} failed integrity check: {check.check_name}"
                            )
        max_latency_ratio_raw = thresholds.get("max_latency_ratio_vs_baseline")
        if max_latency_ratio_raw is not None:
            max_latency_ratio = float(max_latency_ratio_raw)
            for mode, ratio in comparison.performance_vs_baseline.items():
                if ratio > max_latency_ratio:
                    withdrawn.append(
                        f"latency_ratio <= {max_latency_ratio} — "
                        f"{mode} achieved {ratio:.3f}"
                    )
        p0_regressions = (
            ("deterministic integrity check failed — regression detected",)
            if comparison.integrity_regression
            else ()
        )
        if withdrawn or p0_regressions:
            outcome = RecommendationOutcome.NO_GO
            supported_claims: tuple[str, ...] = ()
        else:
            outcome = RecommendationOutcome.GO
            supported_claims = (
                "quality thresholds met for all workflow modes",
                "performance thresholds met for all workflow modes",
                "no deterministic integrity regressions",
                "local-model limitations documented",
            )
        return ReleaseRecommendation(
            schema_version="release-recommendation-v1",
            outcome=outcome.value,
            dataset_version=self.loader.dataset.version,
            comparison=comparison,
            supported_claims=supported_claims,
            withdrawn_claims=tuple(withdrawn),
            known_limitations=tuple(limitations),
            conditions=(),
            p0_regressions=p0_regressions,
        )

    def compare_campaigns(
        self,
        campaign_a: ReleaseBenchmarkResult,
        campaign_b: ReleaseBenchmarkResult,
        *,
        tolerance: float | None = None,
    ) -> ReproducibilityComparison:
        if tolerance is None:
            tolerance = self.config.reproducibility_tolerance
        operational_limit = self.loader.quality_thresholds.get(
            "max_operational_reproducibility_ratio",
            self.config.operational_reproducibility_ratio_limit,
        )
        operational_ratio_limit = float(operational_limit)
        if operational_ratio_limit < 1.0:
            raise ValueError("max_operational_reproducibility_ratio must be >= 1")

        def index_runs(
            result: ReleaseBenchmarkResult,
        ) -> dict[tuple[str, str], CampaignRun]:
            return {(run.mode, run.objective_id): run for run in result.runs}

        idx_a = index_runs(campaign_a)
        idx_b = index_runs(campaign_b)
        quality_tolerances: list[tuple[str, float, float, float]] = []
        performance_tolerances: list[tuple[str, float, float, float]] = []
        details: list[str] = []
        observations: list[str] = []
        all_within = True
        keys_a = set(idx_a)
        keys_b = set(idx_b)
        if keys_a != keys_b:
            all_within = False
            if keys_a - keys_b:
                details.append(
                    f"mode/objective sets differ: missing from B: {sorted(keys_a - keys_b)}"
                )
            if keys_b - keys_a:
                details.append(
                    f"mode/objective sets differ: missing from A: {sorted(keys_b - keys_a)}"
                )
        elif not keys_a:
            all_within = False
            details.append(
                "no runs in either campaign — cannot compare reproducibility"
            )
        for mode, objective_id in sorted(keys_a & keys_b):
            run_a = idx_a[(mode, objective_id)]
            run_b = idx_b[(mode, objective_id)]
            q_status_a = {m.name: m.status for m in run_a.quality_metrics}
            q_status_b = {m.name: m.status for m in run_b.quality_metrics}
            p_status_a = {m.name: m.status for m in run_a.performance_metrics}
            p_status_b = {m.name: m.status for m in run_b.performance_metrics}
            if run_a.quality and run_b.quality:
                for field_name in (
                    "candidate_recall",
                    "source_quality_score",
                    "coverage_completeness",
                    "unsupported_claim_rate",
                    "citation_accuracy",
                    "report_quality_score",
                ):
                    val_a = getattr(run_a.quality, field_name)
                    val_b = getattr(run_b.quality, field_name)
                    status_a = q_status_a.get(field_name, MetricStatus.UNAVAILABLE)
                    status_b = q_status_b.get(field_name, MetricStatus.UNAVAILABLE)
                    if status_a == status_b == MetricStatus.NOT_APPLICABLE:
                        continue
                    if (
                        status_a != MetricStatus.MEASURED
                        or status_b != MetricStatus.MEASURED
                        or val_a is None
                        or val_b is None
                    ):
                        all_within = False
                        details.append(
                            f"{mode}.{objective_id}.{field_name}: not reproducible — "
                            f"A={status_a.value}, B={status_b.value}"
                        )
                        continue
                    denom = abs(val_a) if abs(val_a) > 1e-9 else 1.0
                    rel_diff = abs(val_b - val_a) / denom
                    quality_tolerances.append(
                        (f"{mode}.{objective_id}.{field_name}", val_a, val_b, rel_diff)
                    )
                    if rel_diff > tolerance:
                        all_within = False
                        details.append(
                            f"{mode}.{objective_id}.{field_name}: "
                            f"{val_a:.4f} vs {val_b:.4f} "
                            f"(rel diff {rel_diff:.4f} > {tolerance})"
                        )
            if run_a.performance and run_b.performance:
                for field_name in (
                    "total_latency_ms",
                    "total_tokens",
                    "semantic_calls",
                    "cache_hit_rate",
                    "embedding_throughput",
                    "cpu_percent",
                    "gpu_memory_mb",
                ):
                    val_a = getattr(run_a.performance, field_name)
                    val_b = getattr(run_b.performance, field_name)
                    status_a = p_status_a.get(field_name, MetricStatus.UNAVAILABLE)
                    status_b = p_status_b.get(field_name, MetricStatus.UNAVAILABLE)
                    if status_a == status_b == MetricStatus.NOT_APPLICABLE:
                        continue
                    if (
                        status_a != MetricStatus.MEASURED
                        or status_b != MetricStatus.MEASURED
                        or val_a is None
                        or val_b is None
                    ):
                        all_within = False
                        details.append(
                            f"{mode}.{objective_id}.{field_name}: not reproducible — "
                            f"A={status_a.value}, B={status_b.value}"
                        )
                        continue
                    a = float(val_a)
                    b = float(val_b)
                    denom = abs(a) if abs(a) > 1e-9 else 1.0
                    abs_diff = abs(b - a)
                    rel_diff = abs_diff / denom
                    within = rel_diff <= tolerance
                    failure_limit = f"rel diff {rel_diff:.4f} > {tolerance}"
                    if field_name in OPERATIONAL_PERFORMANCE_METRICS:
                        smaller = min(abs(a), abs(b))
                        larger = max(abs(a), abs(b))
                        ratio = (
                            1.0
                            if larger <= 1e-9
                            else float("inf")
                            if smaller <= 1e-9
                            else larger / smaller
                        )
                        absolute_tolerance = OPERATIONAL_ABSOLUTE_TOLERANCES.get(
                            field_name
                        )
                        within = (
                            within
                            or ratio <= operational_ratio_limit
                            or (
                                absolute_tolerance is not None
                                and abs_diff <= absolute_tolerance
                            )
                        )
                        failure_limit = f"ratio {ratio:.4f} > {operational_ratio_limit}"
                        if absolute_tolerance is not None:
                            failure_limit += (
                                f" and abs diff {abs_diff:.4f} > {absolute_tolerance}"
                            )
                        if within and rel_diff > tolerance:
                            observations.append(
                                f"{mode}.{objective_id}.{field_name}: operational variance "
                                f"accepted by {REPRODUCIBILITY_POLICY_VERSION} — "
                                f"{a:.4f} vs {b:.4f}; rel diff={rel_diff:.4f}; "
                                f"ratio={ratio:.4f}; ratio_limit={operational_ratio_limit}"
                            )
                    performance_tolerances.append(
                        (f"{mode}.{objective_id}.{field_name}", a, b, rel_diff)
                    )
                    if not within:
                        all_within = False
                        details.append(
                            f"{mode}.{objective_id}.{field_name}: "
                            f"{a:.4f} vs {b:.4f} ({failure_limit})"
                        )
        return ReproducibilityComparison(
            run_a_id=campaign_a.campaign_id,
            run_b_id=campaign_b.campaign_id,
            mode="all",
            objective_id="all",
            quality_tolerances=tuple(quality_tolerances),
            performance_tolerances=tuple(performance_tolerances),
            policy_version=REPRODUCIBILITY_POLICY_VERSION,
            relative_tolerance=tolerance,
            operational_ratio_limit=operational_ratio_limit,
            operational_absolute_tolerances=tuple(
                sorted(OPERATIONAL_ABSOLUTE_TOLERANCES.items())
            ),
            all_within_tolerance=all_within,
            observations=tuple(observations),
            details=tuple(details),
        )


def run_release_benchmark(
    dataset: BenchmarkDataset | BenchmarkDatasetLoader,
    database_url: str = "",
    blob_root: Path | str | None = None,
    execution_modes: tuple[str, ...] = RELEASE_MODES,
    strict: bool = False,
    reproducibility_tolerance: float = 0.15,
    operational_reproducibility_ratio_limit: float = 2.0,
    known_limitations: tuple[str, ...] = (),
) -> ReleaseBenchmarkResult:
    loader = (
        dataset
        if isinstance(dataset, BenchmarkDatasetLoader)
        else BenchmarkDatasetLoader(dataset)
    )
    config = ReleaseBenchmarkConfig(
        database_url=database_url,
        blob_root=blob_root,
        execution_modes=execution_modes,
        strict=strict,
        reproducibility_tolerance=reproducibility_tolerance,
        operational_reproducibility_ratio_limit=operational_reproducibility_ratio_limit,
        known_limitations=known_limitations,
    )
    return ReleaseBenchmarkRunner(loader, config).run()
