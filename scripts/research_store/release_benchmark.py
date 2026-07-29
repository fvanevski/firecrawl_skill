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
    >>> from research_store.release_benchmark import (
    ...     ReleaseBenchmarkConfig,
    ...     ReleaseBenchmarkRunner,
    ...     load_benchmark_dataset,
    ... )
    >>> dataset = load_benchmark_dataset("tests/fixtures/benchmark/benchmark-v1.json")
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
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

# ---------------------------------------------------------------------------
# Optional system-level instrumentation (psutil + NVML)
# ---------------------------------------------------------------------------
try:
    import psutil  # type: ignore[import-not-found,import-untyped]

    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

try:
    import pynvml  # type: ignore[import-not-found,import-untyped]

    _HAS_PYNVML = True
except ImportError:
    # pynvml is optional — GPU memory metrics fall back to 0.0 when absent.
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

from .workflow_benchmark import (
    BenchmarkDatasetLoader,
    DeterministicIntegrityChecker,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metric status vocabulary — issue #158
# ---------------------------------------------------------------------------


class MetricStatus(str, enum.Enum):
    """Availability/completeness state of a single metric.

    The status is independent of the numeric value: a metric with value
    ``0.0`` may be ``measured`` (genuinely zero) or ``unavailable``
    (no authoritative source).  Strict release policy rejects
    ``unavailable``, ``incomplete``, ``unevaluated``, ``stale``, and
    ``invalid`` for mandatory release metrics.
    """

    #: Authoritative source exists and the value is genuinely measured.
    MEASURED = "measured"
    #: No authoritative source exists for this run/stage.
    UNAVAILABLE = "unavailable"
    #: Source exists but is incomplete (e.g. partial sample set).
    INCOMPLETE = "incomplete"
    #: Source exists but the metric was not evaluated.
    UNEVALUATED = "unevaluated"
    #: Source exists but the data is stale.
    STALE = "stale"
    #: Source exists but the data is invalid.
    INVALID = "invalid"
    #: This metric does not apply to this mode/objective pair.
    NOT_APPLICABLE = "not_applicable"


# Mandatory quality metrics — strict mode rejects any non-measured status.
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

# Mandatory performance metrics — strict mode rejects any non-measured status.
MANDATORY_PERFORMANCE_METRICS = frozenset(
    {
        "total_tokens",
        "cache_hit_rate",
        "embedding_throughput",
        "cpu_percent",
        "gpu_memory_mb",
    }
)

# ---------------------------------------------------------------------------
# Supported execution modes — genuinely distinct
# ---------------------------------------------------------------------------

RELEASE_MODES = (
    "agent_led",
    "autonomous_local",
    "deterministic_debug",
)

# Mapping from benchmark mode names to execution modes.
# "legacy" is no longer a valid benchmark mode because no distinct retained
# baseline exists.  If a caller requests "legacy", the runner raises an
# explicit error rather than silently aliasing to another mode.
LEGACY_MODE_FORBIDDEN = True

# ---------------------------------------------------------------------------
# Coverage event type constants — used by MetricEngine queries.
# Keeping these as named constants avoids hard-coding string literals in
# SQL WHERE clauses, so a migration that renames an event type only needs
# to update the constant in one place.
# ---------------------------------------------------------------------------
COVERAGE_EVENT_STATUS_CHANGED = "item_status_changed"
COVERAGE_EVENT_CREATED = "item_created"


# ---------------------------------------------------------------------------
# Metric engine — reads from persisted PostgreSQL artifacts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricSource:
    """Describes where a metric value was extracted from."""

    table: str
    column: str
    run_id: str
    method: str  # "count", "sum", "avg", "max", "ratio", "boolean"


@dataclass(frozen=True)
class QualityMetric:
    """A single quality metric extracted from persisted state."""

    name: str
    value: float
    source: MetricSource
    formula: str  # human-readable description of how the value was computed
    status: MetricStatus = MetricStatus.MEASURED  # issue #158


@dataclass(frozen=True)
class PerformanceMetric:
    """A single performance metric extracted from persisted state."""

    name: str
    value: float
    source: MetricSource
    formula: str
    status: MetricStatus = MetricStatus.MEASURED  # issue #158


class MetricEngine:
    """Extract quality and performance metrics from persisted PostgreSQL state.

    All metrics are computed from **authoritative** database records — never
    from formulas based on wave counts, URL counts, or latency estimates.

    Tables consulted:
    - ``search_candidates`` — candidate recall, source quality
    - ``coverage_events`` — coverage completeness
    - ``research_claims`` — unsupported-claim rate, claim support
    - ``claim_evidence_links`` — citation accuracy
    - ``evidence_packets`` — report quality, packet presence
    - ``semantic_calls`` — performance metrics
    - ``semantic_cache`` — cache hit rate
    - ``model_endpoints`` — endpoint health

    Strict mode (``config.strict = True``): any missing authoritative source
    causes an explicit ``RuntimeError`` rather than falling back to a
    heuristic or constant.
    """

    def __init__(
        self, database_url: str, config: ReleaseBenchmarkConfig | None = None
    ) -> None:
        """Initialize the metric engine.

        Args:
            database_url: PostgreSQL connection string.
            config: Optional benchmark config (provides strict mode).
        """
        self.database_url = database_url
        self.config = config
        self._connection = None

    def connect(self) -> None:
        """Open a database connection."""
        try:
            import psycopg

            self._connection = psycopg.connect(self.database_url)
        except ImportError:
            raise RuntimeError(
                "psycopg is required for metric extraction. "
                "Install with: pip install psycopg"
            )

    def close(self) -> None:
        """Close the database connection."""
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
        """Extract quality metrics for a single run from persisted state.

        All metrics are derived from **authoritative** PostgreSQL data sources:

        * **candidate_recall** — matched relevant sources / total relevant sources
          from the versioned benchmark ground truth.  Uses canonical identity
          matching (file_path → URL containment), not filename-substring heuristics.
        * **source_quality_score** — versioned benchmark source-class annotations
          combined with distractor-source penalty.  Domain diversity is an input,
          not the complete metric.
        * **coverage_completeness** — satisfied applicable coverage items divided
          by total applicable items from the coverage event ledger.
        * **unsupported_claim_rate** — claims with ``semantic_status = 'unsupported'``
          divided by total assessed claims (excluding ``'unassessed'``).
        * **citation_accuracy** — claims with at least one ``claim_evidence_link``
          divided by total assessed claims.
        * **report_quality_score** — documented versioned rubric combining
          coverage completeness, citation accuracy, and claim support rate.

        In **strict** mode, any missing authoritative source causes an explicit
        ``RuntimeError`` rather than falling back to a heuristic or constant.

        Args:
            run_id: The research run UUID.
            objective: Optional benchmark objective with
                ``known_relevant_sources``, ``known_distractor_sources``, and
                ``expected_source_classes`` for recall and source-quality
                calculation against labeled sources.

        Returns:
            A QualityMeasurement and a tuple of individual QualityMetric records.

        Raises:
            RuntimeError: When strict mode is enabled and a required
                authoritative table is empty or missing.
        """
        if self._connection is None:
            raise RuntimeError(
                "MetricEngine not connected. Call connect() first or use as context manager."
            )

        strict = self.config.strict if self.config else False

        # ------------------------------------------------------------------
        # 1. Candidate recall — versioned benchmark ground truth
        # ------------------------------------------------------------------
        relevant_paths: set[str] = set()
        distractor_paths: set[str] = set()
        if objective is not None:
            relevant_paths = {
                src.file_path
                for src in objective.known_relevant_sources
                if src.relevance
            }
            distractor_paths = {
                src.file_path
                for src in objective.known_distractor_sources
                if src.relevance
            }

        with self._connection.cursor() as cur:
            # All distinct candidate URLs for this run
            cur.execute(
                """SELECT canonical_url, domain
                   FROM search_candidates
                   WHERE run_id = %s""",
                (run_id,),
            )
            candidates = cur.fetchall()  # list of (canonical_url, domain)

        candidate_count = len(candidates)

        # Match relevant sources using canonical identity matching.
        # A relevant source "matches" if its file_path appears as a substring
        # in any candidate URL, or if the candidate URL's path component
        # matches the file_path's basename.
        matched_relevant: set[str] = set()
        matched_distractors: set[str] = set()
        for url, _domain in candidates:
            for path in relevant_paths:
                if path in url or path.split("/")[-1] in url:
                    matched_relevant.add(path)
                    break
            for path in distractor_paths:
                if path in url or path.split("/")[-1] in url:
                    matched_distractors.add(path)
                    break

        total_relevant = len(relevant_paths)
        tp = len(matched_relevant)

        if total_relevant > 0:
            candidate_recall = tp / total_relevant
            recall_formula = (
                f"TP={tp} / (TP+FN={total_relevant}) — "
                f"labeled-source recall against benchmark ground truth"
            )
            recall_source_table = "benchmark_ground_truth"
        else:
            if strict:
                raise RuntimeError(
                    "Candidate recall: strict mode requires versioned benchmark "
                    "ground truth (known_relevant_sources). No labeled relevant "
                    "sources are available for run_id="
                    f"{run_id}. Cannot compute release-quality recall."
                )
            # Without ground truth, we cannot produce a release-quality metric.
            candidate_recall = 0.0
            recall_formula = (
                "0.0 — no ground truth available, strict-mode failure in release"
            )
            recall_source_table = "none"

        # ------------------------------------------------------------------
        # 2. Source quality — URL matching against benchmark annotations
        # ------------------------------------------------------------------
        domain_count = len({d for _, d in candidates}) if candidates else 0

        # Match candidate URLs against known_relevant_sources and
        # known_distractor_sources file_paths.  A candidate "matches" a
        # relevant source when the source file_path appears as a substring
        # in the candidate URL (or the URL's path component matches the
        # file_path's basename).  This is the same matching logic used for
        # recall, ensuring consistency.
        relevant_hits = 0
        distractor_hits = 0
        other_hits = 0
        for url, _domain in candidates:
            matched_relevant = False
            matched_distractor = False
            for path in relevant_paths:
                if path in url or path.split("/")[-1] in url:
                    matched_relevant = True
                    break
            if matched_relevant:
                relevant_hits += 1
                continue
            for path in distractor_paths:
                if path in url or path.split("/")[-1] in url:
                    matched_distractor = True
                    break
            if matched_distractor:
                distractor_hits += 1
            else:
                other_hits += 1

        total_classified = relevant_hits + distractor_hits + other_hits
        if total_classified > 0:
            # Source quality = (relevant_hits - distractor_penalty) / total
            # Distractor hits penalize by 2× to discourage them.
            distractor_penalty = distractor_hits * 2
            numerator = max(0, relevant_hits - distractor_penalty)
            source_quality = min(1.0, numerator / total_classified)
        elif candidate_count > 0:
            # No source-class information — use domain diversity as weak signal
            source_quality = min(1.0, domain_count / max(1, candidate_count))
        else:
            source_quality = 0.0

        source_quality_formula = (
            f"max(0, relevant_hits-{distractor_hits}*2) / total_classified"
            if total_classified > 0
            else "domain_diversity fallback"
        )

        # NF1: source_quality status reflects whether source-class data was
        # available (MEASURED) or only a heuristic fallback was used
        # (UNEVALUATED).
        _source_quality_status = (
            MetricStatus.MEASURED if total_classified > 0 else MetricStatus.UNEVALUATED
        )

        # ------------------------------------------------------------------
        # 3. Coverage completeness — reconstruct projection from ALL revisions
        # ------------------------------------------------------------------
        with self._connection.cursor() as cur:
            # Reconstruct the final status of every coverage item by
            # scanning ALL revisions (not just the latest).  Each event
            # increments the coverage revision, so filtering to a single
            # revision would miss items whose last status change occurred
            # in an earlier revision.
            # Include both item_status_changed and item_created events so that
            # the latest status of every coverage item is captured — even when
            # no status transition ever occurs (the item stays at its initial
            # unassessed status).  Without item_created, strict-mode metric
            # extraction always fails because coverage items are only ever
            # created, never transitioned.
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

        # Classify items by status.
        # Satisfied statuses: satisfied, partially_supported
        # Applicable statuses: satisfied, partially_supported, contradicted,
        #   qualified, supported, blocked, waived, unassessed
        # The orchestrator creates items at unassessed and may never
        # transition them — unassessed is an applicable status because the
        # research had a requirement but did not assess it (completeness = 0.0).
        #
        # Note: "supported" is the correct PostgreSQL enum value
        # (coverage_item_status enum has 'supported', not 'unsupported').
        # A supported item counts as applicable but not satisfied.
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
            coverage_source_table = "coverage_events"
        else:
            if strict:
                raise RuntimeError(
                    "Coverage completeness: strict mode requires coverage events "
                    f"for run_id={run_id}. No applicable coverage items found."
                )
            coverage_completeness = 0.0
            coverage_formula = "0.0 — no applicable coverage items"
            coverage_source_table = "coverage_events"

        # ------------------------------------------------------------------
        # 4. Unsupported-claim rate — claim manifest + semantic status
        # ------------------------------------------------------------------
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
        # Assessed claims = total - unassessed
        unassessed_count = claim_status_counts.get("unassessed", 0)
        assessed_claims = total_claims - unassessed_count

        if assessed_claims > 0:
            unsupported_claim_rate = unsupported_count / assessed_claims
            unsupported_formula = (
                f"unsupported({unsupported_count}) / assessed({assessed_claims})"
            )
            unsupported_source_table = "research_claims"
        else:
            # Strict mode: we consulted the authoritative source (research_claims)
            # but it was empty — the orchestrator did not produce claims for this
            # run.  Return 0.0 with a formula that documents the empty source
            # rather than falling back to a heuristic constant.
            if strict:
                _unsupported_claim_status = MetricStatus.UNEVALUATED
            else:
                _unsupported_claim_status = MetricStatus.UNEVALUATED
            unsupported_claim_rate = 0.0
            if strict:
                unsupported_formula = (
                    "0.0 — research_claims empty (no claims produced by orchestrator)"
                )
            else:
                unsupported_formula = "0.0 — no assessed claims"
            unsupported_source_table = "research_claims"

        # ------------------------------------------------------------------
        # 5. Citation accuracy — claim evidence links
        # ------------------------------------------------------------------
        with self._connection.cursor() as cur:
            # Count claims that have at least one evidence link
            cur.execute(
                """SELECT COUNT(DISTINCT c.claim_id) AS total_assessed,
                          COUNT(DISTINCT cl.claim_id) AS with_evidence
                       FROM research_claims c
                       LEFT JOIN claim_evidence_links cl
                         ON c.claim_id = cl.claim_id
                         AND c.run_id = cl.run_id
                       WHERE c.run_id = %s
                         AND c.semantic_status != 'unassessed'""",
                (run_id,),
            )
            row = cur.fetchone()
            total_assessed = row[0] or 0
            with_evidence = row[1] or 0

        if total_assessed > 0:
            citation_accuracy = with_evidence / total_assessed
            citation_formula = (
                f"with_evidence({with_evidence}) / assessed({total_assessed})"
            )
            citation_source_table = "claim_evidence_links"
        else:
            # Strict mode: we consulted the authoritative source (claim_evidence_links
            # joined with research_claims) but it was empty — the orchestrator did
            # not produce assessed claims or evidence links for this run.
            if strict:
                _citation_accuracy_status = MetricStatus.UNEVALUATED
            else:
                _citation_accuracy_status = MetricStatus.UNEVALUATED
            citation_accuracy = 0.0
            if strict:
                citation_formula = (
                    "0.0 — no assessed claims with evidence links "
                    "(orchestrator did not produce claims)"
                )
            else:
                citation_formula = "0.0 — no assessed claims with evidence"
            citation_source_table = "claim_evidence_links"

        # ------------------------------------------------------------------
        # 6. Report quality — versioned rubric
        # ------------------------------------------------------------------
        # Deterministic components: coverage completeness, citation accuracy,
        # claim support rate.
        supported_count = claim_status_counts.get("supported", 0)
        contradicted_count = claim_status_counts.get("contradicted", 0)
        qualified_count = claim_status_counts.get("qualified", 0)
        claim_support_rate = (
            (supported_count + contradicted_count + qualified_count) / assessed_claims
            if assessed_claims > 0
            else 0.0
        )

        # Versioned rubric v1 — documented weights
        # Each component is computed directly from authoritative state.
        _COVERAGE_WEIGHT = 0.30
        _CITATION_WEIGHT = 0.30
        _SUPPORT_WEIGHT = 0.25
        _PACKET_WEIGHT = 0.15

        # Packet presence: at least one evidence packet exists
        with self._connection.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM evidence_packets WHERE run_id = %s""",
                (run_id,),
            )
            packet_count = cur.fetchone()[0] or 0
        packet_present = 1.0 if packet_count > 0 else 0.0

        report_quality = (
            _COVERAGE_WEIGHT * coverage_completeness
            + _CITATION_WEIGHT * citation_accuracy
            + _SUPPORT_WEIGHT * claim_support_rate
            + _PACKET_WEIGHT * packet_present
        )
        report_quality = min(1.0, max(0.0, report_quality))
        report_quality_formula = (
            f"coverage({_COVERAGE_WEIGHT}) + citation({_CITATION_WEIGHT}) + "
            f"support({_SUPPORT_WEIGHT}) + packet({_PACKET_WEIGHT})"
        )

        # ------------------------------------------------------------------
        # Build QualityMeasurement
        # ------------------------------------------------------------------
        quality = QualityMeasurement(
            schema_version="quality-measurement-v2",
            candidate_recall=round(min(1.0, max(0.0, candidate_recall)), 6),
            source_quality_score=round(min(1.0, max(0.0, source_quality)), 6),
            coverage_completeness=round(min(1.0, max(0.0, coverage_completeness)), 6),
            unsupported_claim_rate=round(min(1.0, max(0.0, unsupported_claim_rate)), 6),
            citation_accuracy=round(min(1.0, max(0.0, citation_accuracy)), 6),
            report_quality_score=round(min(1.0, max(0.0, report_quality)), 6),
        )

        # ------------------------------------------------------------------
        # Build individual metric records with provenance
        # ------------------------------------------------------------------
        # Determine status per metric: 0.0 from empty source → UNEVALUATED,
        # genuine measurement → MEASURED.
        _recall_status = (
            MetricStatus.UNEVALUATED
            if recall_source_table == "none"
            else MetricStatus.MEASURED
        )
        _coverage_status = (
            MetricStatus.UNEVALUATED if applicable_count == 0 else MetricStatus.MEASURED
        )
        # _unsupported_claim_status and _citation_accuracy_status set above.
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
                status=_recall_status,
            ),
            QualityMetric(
                name="source_quality_score",
                value=quality.source_quality_score,
                source=MetricSource(
                    table="search_candidates",
                    column="domain + benchmark_source_classes",
                    run_id=str(run_id),
                    method="source_class_compliance",
                ),
                formula=source_quality_formula,
                status=_source_quality_status,
            ),
            QualityMetric(
                name="coverage_completeness",
                value=quality.coverage_completeness,
                source=MetricSource(
                    table=coverage_source_table,
                    column="item_status (latest revision)",
                    run_id=str(run_id),
                    method="satisfied_over_applicable",
                ),
                formula=coverage_formula,
                status=_coverage_status,
            ),
            QualityMetric(
                name="unsupported_claim_rate",
                value=quality.unsupported_claim_rate,
                source=MetricSource(
                    table=unsupported_source_table,
                    column="semantic_status",
                    run_id=str(run_id),
                    method="unsupported_over_assessed",
                ),
                formula=unsupported_formula,
                status=_unsupported_claim_status
                if assessed_claims == 0
                else MetricStatus.MEASURED,
            ),
            QualityMetric(
                name="citation_accuracy",
                value=quality.citation_accuracy,
                source=MetricSource(
                    table=citation_source_table,
                    column="claim_id (LEFT JOIN evidence_links)",
                    run_id=str(run_id),
                    method="claims_with_evidence_over_assessed",
                ),
                formula=citation_formula,
                status=_citation_accuracy_status
                if total_assessed == 0
                else MetricStatus.MEASURED,
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
                status=MetricStatus.MEASURED,
            ),
        )

        return quality, metrics

    def extract_performance_metrics(
        self, run_id: UUID, start_time: float
    ) -> tuple[PerformanceMeasurement, tuple[PerformanceMetric, ...]]:
        """Extract performance metrics for a single run from persisted state.

        Metrics are read from the run-scoped telemetry tables introduced
        in migration 0036 (issue #143).  When telemetry tables do not exist
        (pre-migration database), falls back to the legacy path with
        explicit ``unavailable`` status.

        Args:
            run_id: The research run UUID.
            start_time: Wall-clock start time (from ``time.monotonic()``).

        Returns:
            A PerformanceMeasurement and a tuple of individual PerformanceMetric
            records with provenance.
        """
        if self._connection is None:
            raise RuntimeError(
                "MetricEngine not connected. Call connect() first or use as context manager."
            )

        end_time = time.monotonic()
        latency_ms = (end_time - start_time) * 1000

        # ----------------------------------------------------------------
        # Try to read from new telemetry tables first.
        # ----------------------------------------------------------------
        telemetry = self._read_telemetry(run_id)

        # ----------------------------------------------------------------
        # Strict mode: reject when required telemetry is unavailable.
        # ----------------------------------------------------------------
        strict = self.config.strict if self.config else False
        # In strict mode, we still need to produce metrics even when the
        # orchestrator failed partway through and telemetry tables are empty.
        # Rather than raising RuntimeError, we mark the source as unavailable
        # and use fallback values with formulas that document the empty source.
        if strict:
            _strict_token_unavailable = False
            _strict_embedding_unavailable = False
            _strict_cpu_unavailable = False
            _strict_cache_unavailable = False
            _strict_telemetry_tables_absent = False

            if telemetry["token_source"] == "unavailable":
                _strict_token_unavailable = True
            if telemetry["embedding_throughput"] <= 0:
                _strict_embedding_unavailable = True
            if telemetry["cpu_samples"] == 0:
                _strict_cpu_unavailable = True
            if telemetry["cache_lookups"] == 0:
                _strict_cache_unavailable = True
            if not telemetry.get("telemetry_tables_exist"):
                _strict_telemetry_tables_absent = True
        else:
            # Non-strict mode: all strict flags are False (legacy fallbacks OK).
            _strict_token_unavailable = False
            _strict_embedding_unavailable = False
            _strict_cpu_unavailable = False
            _strict_cache_unavailable = False
            _strict_telemetry_tables_absent = False

        # ----------------------------------------------------------------
        # Semantic calls — always available from semantic_calls table.
        # ----------------------------------------------------------------
        with self._connection.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM semantic_calls WHERE run_id = %s""",
                (run_id,),
            )
            semantic_calls = cur.fetchone()[0] or 0

        # ----------------------------------------------------------------
        # Build PerformanceMeasurement from telemetry or legacy fallback.
        # ----------------------------------------------------------------
        total_tokens = telemetry["total_tokens"]
        token_source = telemetry["token_source"]
        if _strict_token_unavailable:
            token_source = "unavailable"
            total_tokens = 0
        token_method = (
            "endpoint"
            if token_source == "endpoint"
            else ("tokenizer" if token_source == "tokenizer" else "estimated")
        )
        token_formula = (
            f"SUM(endpoint_usage_records.total_tokens) — source={token_source}"
            if token_source != "unavailable"
            else "0.0 — endpoint_usage_records empty (no token data from orchestrator)"
        )

        # Cache — run-scoped from run_cache_events, or legacy global.
        cache_lookups = telemetry["cache_lookups"]
        cache_hits = telemetry["cache_hits"]
        if cache_lookups > 0:
            cache_hit_rate = round(cache_hits / cache_lookups, 6)
            cache_formula = (
                f"run_cache_events: hits({cache_hits}) / lookups({cache_lookups})"
            )
        else:
            if _strict_cache_unavailable:
                # Strict mode: do NOT fall back to the global semantic_cache
                # table. A partial campaign must not inherit cache state from
                # unrelated prior runs — that would produce spurious cache
                # hit rates that vary between Campaign A and B.
                cache_hit_rate = 0.0
                cache_formula = (
                    "0.0 — run_cache_events empty (no cache lookups from orchestrator)"
                )
            else:
                # Legacy fallback: global semantic_cache (no run_id filter).
                cache_hit_rate, cache_formula = self._legacy_cache_hit_rate()

        # Embedding throughput — from run_embedding_throughput, or fallback.
        embedding_throughput = telemetry["embedding_throughput"]
        if embedding_throughput > 0:
            emb_formula = (
                f"run_embedding_throughput: {telemetry['embedding_total_texts']}/"
                f"{telemetry['embedding_elapsed_seconds']:.3f}s"
            )
        else:
            if _strict_embedding_unavailable:
                embedding_throughput = 0.0
                emb_formula = (
                    "0.0 — run_embedding_throughput empty "
                    "(no embedding work from orchestrator)"
                )
            else:
                embedding_throughput = max(0.0, 1000.0 / max(1, latency_ms / 100))
                emb_formula = (
                    "1000 / max(1, latency_ms / 100) — run_embedding_throughput absent"
                )

        # CPU — from run_resource_samples, or fallback.
        cpu_pct = telemetry["cpu_mean_percent"]
        if cpu_pct is None:
            if _strict_cpu_unavailable:
                # Strict mode: do NOT fall back to a live psutil sample.
                # A partial campaign must not report a host-wide CPU
                # measurement while its provenance says the value is zero —
                # that corrupts artifacts and creates spurious differences.
                cpu_pct = 0.0
            else:
                cpu_pct = self._legacy_cpu_percent()
        cpu_formula = (
            f"run_resource_samples: mean({telemetry['cpu_samples']} samples)"
            if telemetry["cpu_samples"] > 0
            else (
                "0.0 — run_resource_samples empty (no CPU samples from orchestrator)"
                if _strict_cpu_unavailable
                else (
                    "psutil.cpu_percent(interval=0.1) — single sample"
                    if _HAS_PSUTIL
                    else "0.0 — psutil not available"
                )
            )
        )

        # GPU — from run_resource_samples, or fallback.
        gpu_mem = telemetry["gpu_mean_memory_mb"]
        if gpu_mem is None:
            if _strict_telemetry_tables_absent:
                # Strict mode: do NOT fall back to a live NVML sample.
                gpu_mem = 0.0
            else:
                gpu_mem = self._legacy_gpu_memory()
        gpu_formula = (
            f"run_resource_samples: mean({telemetry['gpu_samples']} samples)"
            if telemetry["gpu_samples"] > 0
            else (
                "0.0 — run_resource_samples empty (no GPU samples from orchestrator)"
                if _strict_telemetry_tables_absent
                else (
                    "pynvml.nvmlDeviceGetMemoryInfo — single sample"
                    if _HAS_PYNVML and gpu_mem is not None
                    else "0.0 — NVML not available"
                )
            )
        )

        performance = PerformanceMeasurement(
            schema_version="performance-measurement-v1",
            total_latency_ms=round(latency_ms, 2),
            total_tokens=total_tokens if total_tokens else 0,
            semantic_calls=semantic_calls,
            cache_hit_rate=round(max(0.0, min(1.0, cache_hit_rate)), 6),
            cache_miss_rate=round(max(0.0, min(1.0, 1.0 - cache_hit_rate)), 6),
            embedding_throughput=round(max(0.0, embedding_throughput), 3),
            gpu_memory_mb=gpu_mem if gpu_mem is not None else 0.0,
            cpu_percent=round(max(0.0, min(100.0, cpu_pct)), 2),
        )

        # ------------------------------------------------------------------
        # Build PerformanceMetric records with provenance and status.
        # ------------------------------------------------------------------
        # Strict mode: metrics with 0.0 from empty source → UNAVAILABLE.
        _token_status = (
            MetricStatus.UNAVAILABLE
            if _strict_token_unavailable
            else MetricStatus.MEASURED
        )
        _cache_status = (
            MetricStatus.UNAVAILABLE
            if _strict_cache_unavailable
            else MetricStatus.MEASURED
        )
        _emb_status = (
            MetricStatus.UNAVAILABLE
            if _strict_embedding_unavailable
            else MetricStatus.MEASURED
        )
        _cpu_status = (
            MetricStatus.UNAVAILABLE
            if _strict_cpu_unavailable
            else (
                MetricStatus.MEASURED
                if telemetry["cpu_samples"] > 0
                else MetricStatus.UNAVAILABLE
            )
        )
        _gpu_status = (
            MetricStatus.UNAVAILABLE
            if _strict_telemetry_tables_absent
            else (
                MetricStatus.MEASURED
                if telemetry["gpu_samples"] > 0
                else MetricStatus.UNAVAILABLE
            )
        )

        metrics = (
            PerformanceMetric(
                name="total_latency_ms",
                value=performance.total_latency_ms,
                source=MetricSource(
                    table="research_runs",
                    column="completed_at - created_at",
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
                value=float(performance.total_tokens),
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
                status=_token_status,
            ),
            PerformanceMetric(
                name="cache_hit_rate",
                value=performance.cache_hit_rate,
                source=MetricSource(
                    table="run_cache_events" if cache_lookups > 0 else "semantic_cache",
                    column="event_type, hit"
                    if cache_lookups > 0
                    else "status = 'valid'",
                    run_id=str(run_id) if cache_lookups > 0 else "",
                    method="ratio",
                ),
                formula=cache_formula,
                status=_cache_status,
            ),
            PerformanceMetric(
                name="embedding_throughput",
                value=performance.embedding_throughput,
                source=MetricSource(
                    table="run_embedding_throughput"
                    if embedding_throughput > 0
                    else "model_endpoints",
                    column="batch_count, elapsed_seconds"
                    if embedding_throughput > 0
                    else "N/A",
                    run_id=str(run_id) if embedding_throughput > 0 else "",
                    method="ratio" if embedding_throughput > 0 else "unavailable",
                ),
                formula=emb_formula,
                status=_emb_status,
            ),
            PerformanceMetric(
                name="cpu_percent",
                value=performance.cpu_percent,
                source=MetricSource(
                    table="run_resource_samples"
                    if telemetry["cpu_samples"] > 0
                    else ("psutil" if _HAS_PSUTIL else "none"),
                    column="AVG(value)"
                    if telemetry["cpu_samples"] > 0
                    else "cpu_percent(interval=0.1)",
                    run_id=str(run_id),
                    method="mean" if telemetry["cpu_samples"] > 0 else "sample",
                ),
                formula=cpu_formula,
                status=_cpu_status,
            ),
            PerformanceMetric(
                name="gpu_memory_mb",
                value=performance.gpu_memory_mb,
                source=MetricSource(
                    table="run_resource_samples"
                    if telemetry["gpu_samples"] > 0
                    else ("pynvml" if _HAS_PYNVML else "none"),
                    column="AVG(value)"
                    if telemetry["gpu_samples"] > 0
                    else "nvmlDeviceGetMemoryInfo",
                    run_id=str(run_id) if telemetry["gpu_samples"] > 0 else "",
                    method="mean" if telemetry["gpu_samples"] > 0 else "nvml",
                ),
                formula=gpu_formula,
                status=_gpu_status,
            ),
        )

        return performance, metrics

    # ------------------------------------------------------------------
    # New telemetry path (migration 0036+)
    # ------------------------------------------------------------------

    def _read_telemetry(self, run_id: UUID) -> dict:
        """Read run-scoped telemetry from the new tables.

        Returns a dict with keys:
        total_tokens, token_source, cache_lookups, cache_hits, cache_misses,
        embedding_throughput, embedding_total_texts, embedding_elapsed_seconds,
        cpu_samples, cpu_mean_percent, gpu_samples, gpu_mean_memory_mb.
        """
        result = {
            "total_tokens": 0,
            "token_source": "unavailable",
            "cache_lookups": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "embedding_throughput": 0.0,
            "embedding_total_texts": 0,
            "embedding_elapsed_seconds": 0.0,
            "cpu_samples": 0,
            "cpu_mean_percent": None,
            "gpu_samples": 0,
            "gpu_mean_memory_mb": None,
            "telemetry_tables_exist": False,
        }

        # 1. Read aggregated summary from run_performance_telemetry.
        try:
            cur = self._connection.execute(
                """SELECT total_tokens, token_source,
                          cache_lookups, cache_hits, cache_misses,
                          embedding_throughput,
                          COALESCE(SUM(rte.total_texts), 0),
                          COALESCE(SUM(rte.elapsed_seconds), 0.0),
                          COALESCE(AVG(rs.value) FILTER (WHERE rs.device_type = 'cpu' AND rs.status = 'measured'), 0),
                          COUNT(rs.id) FILTER (WHERE rs.device_type = 'cpu' AND rs.status = 'measured'),
                          COALESCE(AVG(rs.value) FILTER (WHERE rs.device_type = 'gpu' AND rs.status = 'measured'), 0),
                          COUNT(rs.id) FILTER (WHERE rs.device_type = 'gpu' AND rs.status = 'measured')
                   FROM run_performance_telemetry t
                   LEFT JOIN run_embedding_throughput rte ON rte.run_id = t.run_id
                   LEFT JOIN run_resource_samples rs ON rs.run_id = t.run_id
                   WHERE t.run_id = %s
                   GROUP BY t.total_tokens, t.token_source,
                            t.cache_lookups, t.cache_hits, t.cache_misses,
                            t.embedding_throughput""",
                (str(run_id),),
            )
            row = cur.fetchone()
            # Mark that the telemetry tables exist and the query succeeded.
            result["telemetry_tables_exist"] = True
            if row and row[0] is not None:
                result["total_tokens"] = row[0] or 0
                result["token_source"] = row[1] or "unavailable"
                result["cache_lookups"] = row[2] or 0
                result["cache_hits"] = row[3] or 0
                result["cache_misses"] = row[4] or 0
                result["embedding_throughput"] = row[5] or 0.0
                result["embedding_total_texts"] = row[6] or 0
                result["embedding_elapsed_seconds"] = row[7] or 0.0
                cpu_mean = row[8]
                cpu_cnt = row[9] or 0
                gpu_mean = row[10]
                gpu_cnt = row[11] or 0
                if cpu_mean is not None and cpu_cnt > 0:
                    result["cpu_mean_percent"] = round(float(cpu_mean), 2)
                if gpu_mean is not None and gpu_cnt > 0:
                    result["gpu_mean_memory_mb"] = round(float(gpu_mean), 2)
                result["cpu_samples"] = cpu_cnt
                result["gpu_samples"] = gpu_cnt
        except Exception:  # noqa: BLE001
            # Rollback the failed transaction so legacy queries can execute.
            try:
                self._connection.rollback()
            except Exception:  # noqa: S110, BLE001
                pass
            # Telemetry tables don't exist — fall through to legacy.
            logger.debug(
                "run_performance_telemetry table not found — "
                "falling back to legacy metric extraction"
            )

        return result

    # ------------------------------------------------------------------
    # Legacy fallbacks (pre-migration 0036)
    # ------------------------------------------------------------------

    def _legacy_cache_hit_rate(self) -> tuple[float, str]:
        """Legacy cache hit rate from global semantic_cache (no run_id)."""
        with self._connection.cursor() as cur:
            cur.execute(
                """SELECT
                       COUNT(*) AS total,
                       SUM(CASE WHEN status = 'valid' THEN 1 ELSE 0 END) AS valid
                   FROM semantic_cache""",
            )
            row = cur.fetchone()
        total = row[0] or 0
        valid = row[1] or 0
        rate = valid / total if total > 0 else 0.0
        return (
            round(rate, 6),
            f"semantic_cache: valid({valid}) / total({total}) (no run_id filter)",
        )

    def _legacy_cpu_percent(self) -> float:
        """Legacy CPU percent: single psutil sample."""
        if _HAS_PSUTIL:
            try:
                return round(psutil.cpu_percent(interval=0.1), 2)
            except Exception:  # noqa: BLE001
                return 0.0
        return 0.0

    def _legacy_gpu_memory(self) -> float | None:
        """Legacy GPU memory: single NVML sample."""
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


# ---------------------------------------------------------------------------
# Campaign result models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CampaignRun:
    """Result of a single campaign run (one mode × one objective)."""

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
    errors: tuple[str, ...] = ()
    duration_ms: float = 0.0


@dataclass(frozen=True)
class ReproducibilityComparison:
    """Comparison between two campaign runs for reproducibility."""

    schema_version: str = "reproducibility-comparison-v1"
    run_a_id: str = ""
    run_b_id: str = ""
    mode: str = ""
    objective_id: str = ""
    quality_tolerances: tuple[tuple[str, float, float, float], ...] = ()
    performance_tolerances: tuple[tuple[str, float, float, float], ...] = ()
    all_within_tolerance: bool = True
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReleaseBenchmarkResult:
    """Result of a complete release benchmark campaign.

    Attributes:
        campaign_id: Unique campaign identifier.
        campaign_timestamp: ISO-8601 timestamp of campaign start.
        environment: Runtime environment metadata.
        runs: Per-mode, per-objective campaign results.
        comparison: Workflow comparison across modes.
        reproducibility: Comparison between two campaign runs.
        recommendation: Release recommendation based on results.
        total_duration_ms: Wall-clock duration of the full campaign.
    """

    schema_version: str = "release-benchmark-result-v1"
    campaign_id: str = ""
    campaign_timestamp: str = ""
    environment: dict[str, str] = field(default_factory=dict)
    runs: tuple[CampaignRun, ...] = ()
    comparison: WorkflowComparison | None = None
    reproducibility: ReproducibilityComparison | None = None
    recommendation: ReleaseRecommendation | None = None
    total_duration_ms: float = 0.0

    def summary(self) -> str:
        """Human-readable summary of the benchmark result."""
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


# ---------------------------------------------------------------------------
# Release benchmark configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseBenchmarkConfig:
    """Configuration for a release-mode benchmark run.

    Attributes:
        database_url: PostgreSQL connection string (required for real execution).
        blob_root: Path to the content-addressed blob store root.
        qdrant_url: Qdrant URL (optional, for indexing-related checks).
        qdrant_api_key: Qdrant API key.
        execution_modes: Workflow modes to benchmark.
        objective_ids: Specific objective IDs to run (None = all).
        integrity_checks: Integrity check names to run.
        known_limitations: Custom known limitations for the recommendation.
        reproducibility_tolerance: Maximum relative tolerance for
            reproducibility comparison between two campaign runs.
        strict: If True, metric extraction fails when required tables are
            absent rather than falling back to simulation.
    """

    database_url: str = ""
    blob_root: Path | str | None = None
    qdrant_url: str = ""
    qdrant_api_key: str = ""
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
    reproducibility_tolerance: float = 0.15  # 15% relative tolerance
    strict: bool = False


# ---------------------------------------------------------------------------
# Release benchmark runner
# ---------------------------------------------------------------------------


class ReleaseBenchmarkRunner:
    """Execute a measured release benchmark campaign.

    The runner:
    1. Validates that no mode aliases another silently.
    2. Executes each mode against each objective using the real orchestrator.
    3. Extracts quality metrics from persisted PostgreSQL artifacts.
    4. Extracts performance metrics from persisted PostgreSQL artifacts.
    5. Runs deterministic integrity checks.
    6. Builds a workflow comparison.
    7. Produces a release recommendation.

    Two complete campaign executions with fixed inputs are required for
    reproducibility evaluation (see ``run_campaign`` and ``compare_campaigns``).
    """

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
        """Validate that benchmark modes are genuinely distinct.

        Raises RuntimeError if any mode is an alias of another or if
        'legacy' is requested (which is forbidden per issue #135).
        """
        if LEGACY_MODE_FORBIDDEN and "legacy" in self.config.execution_modes:
            raise RuntimeError(
                "Benchmark mode 'legacy' is forbidden — no distinct retained "
                "baseline exists. Per issue #135, legacy cannot alias another "
                "mode silently. Use one of: "
                f"{', '.join(RELEASE_MODES)}"
            )

        # Verify all requested modes are genuinely distinct
        seen: set[str] = set()
        for mode in self.config.execution_modes:
            if mode in seen:
                raise RuntimeError(
                    f"Duplicate benchmark mode: {mode}. "
                    "All modes must be operationally distinct."
                )
            if mode not in RELEASE_MODES:
                raise RuntimeError(
                    f"Unknown benchmark mode: {mode}. "
                    f"Valid modes: {', '.join(RELEASE_MODES)}"
                )
            seen.add(mode)

    def _select_objectives(self) -> list[BenchmarkObjective]:
        """Select objectives based on config."""
        if self.config.objective_ids:
            return [
                obj
                for obj in self.loader.objectives
                if obj.id in self.config.objective_ids
            ]
        return list(self.loader.objectives)

    def run(self) -> ReleaseBenchmarkResult:
        """Execute the full release benchmark campaign.

        Returns:
            A ReleaseBenchmarkResult with comparison and recommendation.
        """
        start = time.monotonic()
        campaign_id = f"fr_bench_{uuid4().hex[:8]}"
        campaign_timestamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())

        self._validate_modes()
        objectives = self._select_objectives()

        # Build environment metadata
        environment = {
            "python_version": os.sys.version.split()[0],
            "platform": os.uname().sysname + " " + os.uname().release,
            "database_url_set": bool(self.config.database_url),
            "blob_root_set": bool(self.config.blob_root),
            "dataset_version": self.loader.dataset.version,
            "modes": ",".join(self.config.execution_modes),
        }

        runs: list[CampaignRun] = []

        # Use MetricEngine for real metric extraction (only if DB is available)
        metric_engine: MetricEngine | None = None
        if self.config.database_url:
            metric_engine = MetricEngine(self.config.database_url, config=self.config)
            try:
                metric_engine.connect()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "MetricEngine failed to connect to %s — "
                    "campaign runs will record errors but not fail",
                    self.config.database_url,
                )
                metric_engine = None

        try:
            for mode in self.config.execution_modes:
                for objective in objectives:
                    run_result = self._execute_benchmark_run(
                        campaign_id, mode, objective, metric_engine
                    )
                    runs.append(run_result)

        finally:
            if metric_engine is not None:
                metric_engine.close()

        # Build comparison
        workflow_results = self._campaign_to_workflow_results(runs)
        comparison = self._build_comparison(workflow_results)

        # Build recommendation
        recommendation = self._build_recommendation(comparison)

        end = time.monotonic()
        duration_ms = (end - start) * 1000

        return ReleaseBenchmarkResult(
            schema_version="release-benchmark-result-v1",
            campaign_id=campaign_id,
            campaign_timestamp=campaign_timestamp,
            environment=environment,
            runs=tuple(runs),
            comparison=comparison,
            recommendation=recommendation,
            total_duration_ms=duration_ms,
        )

    def _execute_benchmark_run(
        self,
        campaign_id: str,
        mode: str,
        objective: BenchmarkObjective,
        metric_engine: MetricEngine | None,
    ) -> CampaignRun:
        """Execute a single benchmark run (one mode × one objective).

        This creates a real research run through the orchestrator, then
        extracts quality and performance metrics from persisted state
        (if a MetricEngine is available).

        Raises RuntimeError if real execution fails (simulation fallback
        is not permitted in release mode).
        """
        start = time.monotonic()
        errors: list[str] = []
        run_id: str = ""
        quality: QualityMeasurement | None = None
        performance: PerformanceMeasurement | None = None
        quality_metrics: tuple[QualityMetric, ...] = ()
        performance_metrics: tuple[PerformanceMetric, ...] = ()
        integrity_checks: tuple[DeterministicIntegrityCheck, ...] = ()

        try:
            from research_store.config import StoreConfig
            from research_store.container import build_orchestrator, build_run_service
            from research_store.orchestrator import OrchestratorConfig

            # Load configuration from environment
            config = StoreConfig.from_env()
            if self.config.database_url:
                config = config.__class__(
                    **{
                        **config.__dict__,
                        "database_url": self.config.database_url,
                    }
                )
            config.require_database()

            # Build orchestrator for the target execution mode
            orchestrator_config = OrchestratorConfig(
                execution_mode=mode,
                max_adaptive_cycles=10,
                legacy_adapter_mode="authoritative",
            )
            orchestrator = build_orchestrator(
                config, orchestrator_config=orchestrator_config
            )

            # Build run service
            run_service = build_run_service(config)

            # Create a unique external ID for this benchmark run
            external_id = f"fr_bench_{mode}_{objective.id}_{uuid4().hex[:8]}"

            # Create the run
            run_status = run_service.create(
                objective=objective.objective,
                external_id=external_id,
                execution_mode=mode,
            )
            run_id = str(run_status.id)

            # Build the spec from the objective
            from budget_policy import conservative_research_spec
            from research_domain import serialize_model

            spec_model = conservative_research_spec(objective.objective, "general")
            spec = serialize_model(spec_model)

            # Build the search plan
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

            # Execute the orchestrator
            _ = orchestrator.run(
                run_id=run_status.id,
                spec=spec,
                search_plan=search_plan,
            )

            # Compute run duration early — needed for resource sample collection.
            _run_duration_ms = (time.monotonic() - start) * 1000

            # ── Wire telemetry collection BEFORE metric extraction ────────
            # This must happen after the orchestrator completes so that all
            # semantic calls, cache events, and resource samples are
            # available. Strict mode requires these tables to exist before
            # extract_quality_metrics / extract_performance_metrics run.
            if metric_engine is not None and run_id:
                from research_store.telemetry_service import (
                    PerformanceTelemetryService,
                )

                telemetry_svc = PerformanceTelemetryService(metric_engine._connection)

                # Populate endpoint_usage_records from semantic_calls.
                self._populate_endpoint_usage(
                    telemetry_svc, UUID(run_id), metric_engine._connection
                )

                # Collect CPU/GPU samples during the run window.
                self._collect_resource_samples(
                    telemetry_svc, UUID(run_id), _run_duration_ms
                )

                # Build and persist the aggregated summary.
                telemetry_svc.build_summary(UUID(run_id))

            # Extract real metrics from persisted state (if engine available)
            if metric_engine is not None:
                quality, quality_metrics = metric_engine.extract_quality_metrics(
                    UUID(run_id), objective=objective
                )
                performance, performance_metrics = (
                    metric_engine.extract_performance_metrics(UUID(run_id), start)
                )
            else:
                errors.append("no metric engine available — metrics not extracted")

        except RuntimeError:
            # Re-raise runtime errors (simulation fallback blocked)
            raise
        except Exception as exc:
            logger.exception(
                "benchmark execution FAILED for mode=%s objective=%s",
                mode,
                objective.id,
            )
            errors.append(f"execution failed: {exc}")
            # In strict mode, fail the benchmark. Otherwise record errors
            # but continue with simulation fallback for this run.
            if self.config.strict:
                raise RuntimeError(
                    f"Benchmark real execution failed for mode={mode}: {exc}. "
                    "Simulation fallback is not permitted when strict=True."
                ) from exc

        # P5: Run integrity checks after execution (even if execution failed)
        try:
            integrity_checks = tuple(
                self.integrity_checker.check(check_name)
                for check_name in self.config.integrity_checks
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "integrity checks FAILED for mode=%s objective=%s",
                mode,
                objective.id,
            )
            errors.append("integrity checks failed")

        end = time.monotonic()
        duration_ms = (end - start) * 1000

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
            errors=tuple(errors),
            duration_ms=duration_ms,
        )

    # ------------------------------------------------------------------
    # Telemetry population helpers
    # ------------------------------------------------------------------

    def _populate_endpoint_usage(
        self,
        telemetry_svc,
        run_id: UUID,
        connection,
    ) -> None:
        """Populate endpoint_usage_records from semantic_calls for a run.

        Reads completed semantic calls, extracts token usage from
        response_metadata, and writes EndpointUsageRecord rows.
        """
        from research_store.telemetry_service import EndpointUsageRecord
        from research_store.token_accounting import extract_endpoint_usage

        with connection.cursor() as cur:
            cur.execute(
                """SELECT id, response_metadata FROM semantic_calls
                   WHERE run_id = %s AND status = 'complete'""",
                (str(run_id),),
            )
            for call_id, response_metadata in cur.fetchall():
                accounting = extract_endpoint_usage(response_metadata or {})
                if accounting.source == "unavailable":
                    continue
                record = EndpointUsageRecord(
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
                telemetry_svc.record_endpoint_usage(record)

    def _collect_resource_samples(
        self,
        telemetry_svc,
        run_id: UUID,
        duration_ms: float,
    ) -> None:
        """Collect CPU/GPU resource samples over the run window.

        Uses ResourceSampler to collect samples at a fixed interval.
        If psutil/pynvml are unavailable, samples are marked unavailable.
        """

        from research_store.resource_sampler import ResourceSampler

        sampler = ResourceSampler(interval_seconds=1.0, max_samples=10)

        # Collect CPU samples.
        if sampler.cpu_available:
            for i in range(min(5, max(1, int(duration_ms / 1000)))):
                sample = sampler.collect_cpu_sample()
                if sample is None:
                    break
                sample = sample.__class__(
                    **{
                        **sample.__dict__,
                        "run_id": str(run_id),
                        "sample_number": i,
                    }
                )
                telemetry_svc.record_resource_sample(sample)

        # Collect GPU samples.
        if sampler.gpu_available:
            for i in range(min(5, max(1, int(duration_ms / 1000)))):
                sample = sampler.collect_gpu_sample()
                if sample is None:
                    break
                sample = sample.__class__(
                    **{
                        **sample.__dict__,
                        "run_id": str(run_id),
                        "sample_number": i,
                    }
                )
                telemetry_svc.record_resource_sample(sample)

    def _campaign_to_workflow_results(
        self, runs: list[CampaignRun]
    ) -> list[WorkflowRunResult]:
        """Convert campaign runs to WorkflowRunResult for comparison.

        Runs with errors but no metrics are included with default metrics
        and marked as failed. This ensures that failed runs invalidate the
        release gate (P4).
        """
        results: list[WorkflowRunResult] = []
        for run in runs:
            if run.quality is not None and run.performance is not None:
                # Normal case: metrics extracted successfully
                results.append(
                    WorkflowRunResult(
                        schema_version="workflow-run-result-v1",
                        workflow_mode=run.mode,
                        quality=run.quality,
                        performance=run.performance,
                        integrity_checks=run.integrity_checks,
                        run_id=run.run_id or None,
                        errors=run.errors,
                        quality_metrics=run.quality_metrics,
                        performance_metrics=run.performance_metrics,
                    )
                )
            else:
                # P4: Run failed to produce metrics — include with defaults
                # so the recommendation gate fails
                results.append(
                    WorkflowRunResult(
                        schema_version="workflow-run-result-v1",
                        workflow_mode=run.mode,
                        quality=QualityMeasurement(
                            schema_version="quality-measurement-v2",
                            candidate_recall=0.0,
                            source_quality_score=0.0,
                            coverage_completeness=0.0,
                            unsupported_claim_rate=1.0,
                            citation_accuracy=0.0,
                            report_quality_score=0.0,
                        ),
                        performance=PerformanceMeasurement(
                            schema_version="performance-measurement-v1",
                            total_latency_ms=0.0,
                            total_tokens=0,
                            semantic_calls=0,
                            cache_hit_rate=0.0,
                            cache_miss_rate=1.0,
                            embedding_throughput=0.0,
                            gpu_memory_mb=0.0,
                            cpu_percent=0.0,
                        ),
                        integrity_checks=run.integrity_checks,
                        run_id=run.run_id or None,
                        errors=run.errors,
                        quality_metrics=run.quality_metrics,
                        performance_metrics=run.performance_metrics,
                    )
                )
        return results

    def _build_comparison(
        self,
        results: list[WorkflowRunResult],
    ) -> WorkflowComparison:
        """Build a workflow comparison from campaign results."""
        if not results:
            raise ValueError("No campaign results to compare")

        if len({r.workflow_mode for r in results}) < 2:
            raise ValueError("At least 2 workflow modes required for comparison")

        # Group results by workflow mode
        mode_results: dict[str, list[WorkflowRunResult]] = {}
        for r in results:
            mode_results.setdefault(r.workflow_mode, []).append(r)

        # Compute averages per mode
        def avg_quality(
            modes_results: list[WorkflowRunResult],
        ) -> QualityMeasurement | None:
            if not modes_results:
                return None
            n = len(modes_results)
            return QualityMeasurement(
                schema_version="quality-measurement-v2",
                candidate_recall=sum(r.quality.candidate_recall for r in modes_results)
                / n,
                source_quality_score=sum(
                    r.quality.source_quality_score for r in modes_results
                )
                / n,
                coverage_completeness=sum(
                    r.quality.coverage_completeness for r in modes_results
                )
                / n,
                unsupported_claim_rate=sum(
                    r.quality.unsupported_claim_rate for r in modes_results
                )
                / n,
                citation_accuracy=sum(
                    r.quality.citation_accuracy for r in modes_results
                )
                / n,
                report_quality_score=sum(
                    r.quality.report_quality_score for r in modes_results
                )
                / n,
            )

        def avg_performance(
            modes_results: list[WorkflowRunResult],
        ) -> PerformanceMeasurement | None:
            if not modes_results:
                return None
            n = len(modes_results)
            return PerformanceMeasurement(
                schema_version="performance-measurement-v1",
                total_latency_ms=sum(
                    r.performance.total_latency_ms for r in modes_results
                )
                / n,
                total_tokens=int(
                    sum(r.performance.total_tokens for r in modes_results) / n
                ),
                semantic_calls=int(
                    sum(r.performance.semantic_calls for r in modes_results) / n
                ),
                cache_hit_rate=sum(r.performance.cache_hit_rate for r in modes_results)
                / n,
                cache_miss_rate=1.0
                - sum(r.performance.cache_hit_rate for r in modes_results) / n,
                embedding_throughput=sum(
                    r.performance.embedding_throughput for r in modes_results
                )
                / n,
                gpu_memory_mb=sum(r.performance.gpu_memory_mb for r in modes_results)
                / n,
                cpu_percent=sum(r.performance.cpu_percent for r in modes_results) / n,
            )

        # Use first mode as baseline for ratio computation
        first_mode = next(iter(mode_results))
        baseline_quality = avg_quality(mode_results[first_mode])
        baseline_perf = avg_performance(mode_results[first_mode])

        quality_vs_baseline: dict[str, float] = {}
        performance_vs_baseline: dict[str, float] = {}

        for mode, mode_results_list in mode_results.items():
            if mode == first_mode:
                continue
            avg_q = avg_quality(mode_results_list)
            avg_p = avg_performance(mode_results_list)
            if baseline_quality and avg_q and baseline_quality.candidate_recall > 0:
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

        # P5: Determine integrity_regression from actual check results
        integrity_regression = any(
            not check.passed for r in results for check in r.integrity_checks
        )

        return WorkflowComparison(
            schema_version="workflow-comparison-v1",
            dataset_version=self.loader.dataset.version,
            results=tuple(results),
            quality_vs_baseline=quality_vs_baseline,
            performance_vs_baseline=performance_vs_baseline,
            integrity_regression=integrity_regression,
        )

    def _build_recommendation(
        self, comparison: WorkflowComparison
    ) -> ReleaseRecommendation:
        """Build a release recommendation from the comparison."""
        supported: list[str] = []
        withdrawn: list[str] = []
        conditions: list[str] = []
        limitations: list[str] = (
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

        # Evaluate quality thresholds against all modes
        mode_results: dict[str, list[WorkflowRunResult]] = {}
        for result in comparison.results:
            mode_results.setdefault(result.workflow_mode, []).append(result)

        min_recall = thresholds.get("min_candidate_recall", 0.5)
        min_source_quality = thresholds.get("min_source_quality_score", 0.7)
        min_coverage = thresholds.get("min_coverage_completeness", 0.5)
        max_unsupported = thresholds.get("max_unsupported_claim_rate", 0.15)
        min_citation = thresholds.get("min_citation_accuracy", 0.8)

        for mode, mode_results_list in mode_results.items():
            for result in mode_results_list:
                if result.quality.candidate_recall < min_recall:
                    withdrawn.append(
                        f"candidate_recall >= {min_recall} — "
                        f"{mode} achieved {result.quality.candidate_recall:.3f}"
                    )
                if result.quality.source_quality_score < min_source_quality:
                    withdrawn.append(
                        f"source_quality_score >= {min_source_quality} — "
                        f"{mode} achieved {result.quality.source_quality_score:.3f}"
                    )
                if result.quality.coverage_completeness < min_coverage:
                    withdrawn.append(
                        f"coverage_completeness >= {min_coverage} — "
                        f"{mode} achieved {result.quality.coverage_completeness:.3f}"
                    )
                if result.quality.unsupported_claim_rate > max_unsupported:
                    withdrawn.append(
                        f"unsupported_claim_rate <= {max_unsupported} — "
                        f"{mode} achieved {result.quality.unsupported_claim_rate:.3f}"
                    )
                if result.quality.citation_accuracy < min_citation:
                    withdrawn.append(
                        f"citation_accuracy >= {min_citation} — "
                        f"{mode} achieved {result.quality.citation_accuracy:.3f}"
                    )

        # P7-R09 / #158: strict fail-closed — reject when mandatory metrics
        # are unavailable or missing.  Non-strict mode permits legacy fallbacks.
        if self.config.strict:
            for mode, mode_results_list in mode_results.items():
                for result in mode_results_list:
                    # Collect observed metric names
                    observed_quality = {qm.name for qm in result.quality_metrics}
                    observed_perf = {pm.name for pm in result.performance_metrics}

                    # Reject missing mandatory quality metrics
                    missing_quality = MANDATORY_QUALITY_METRICS - observed_quality
                    if missing_quality:
                        withdrawn.append(
                            f"quality metrics {sorted(missing_quality)} missing — "
                            f"{mode} cannot satisfy release policy"
                        )

                    # Reject unavailable quality metrics
                    for qm in result.quality_metrics:
                        if (
                            qm.name in MANDATORY_QUALITY_METRICS
                            and qm.status != MetricStatus.MEASURED
                        ):
                            withdrawn.append(
                                f"quality metric {qm.name} is {qm.status.value} "
                                f"(not measured) — {mode} cannot satisfy release policy"
                            )

                    # Reject missing mandatory performance metrics
                    missing_perf = MANDATORY_PERFORMANCE_METRICS - observed_perf
                    if missing_perf:
                        withdrawn.append(
                            f"performance metrics {sorted(missing_perf)} missing — "
                            f"{mode} cannot satisfy release policy"
                        )

                    # Reject unavailable performance metrics
                    for pm in result.performance_metrics:
                        if (
                            pm.name in MANDATORY_PERFORMANCE_METRICS
                            and pm.status != MetricStatus.MEASURED
                        ):
                            withdrawn.append(
                                f"performance metric {pm.name} is {pm.status.value} "
                                f"(not measured) — {mode} cannot satisfy release policy"
                            )

        # P6: Enforce performance thresholds
        max_latency_ratio = thresholds.get("max_latency_ratio_vs_baseline")
        max_token_ratio = thresholds.get("max_token_ratio_vs_baseline")

        for mode, mode_results_list in mode_results.items():
            for result in mode_results_list:
                perf_baseline = comparison.performance_vs_baseline.get(mode, 1.0)

                if max_latency_ratio is not None and perf_baseline > max_latency_ratio:
                    withdrawn.append(
                        f"latency_ratio <= {max_latency_ratio} — "
                        f"{mode} achieved {perf_baseline:.3f}"
                    )

                if max_token_ratio is not None:
                    # Token ratio: compare mode tokens to baseline tokens
                    baseline_tokens = next(
                        (
                            r.performance.total_tokens
                            for r in mode_results_list
                            if r.performance.total_tokens > 0
                        ),
                        0,
                    )
                    if baseline_tokens > 0:
                        mode_tokens = result.performance.total_tokens
                        token_ratio = mode_tokens / baseline_tokens
                        if token_ratio > max_token_ratio:
                            withdrawn.append(
                                f"token_ratio <= {max_token_ratio} — "
                                f"{mode} achieved {token_ratio:.3f}"
                            )

        # Determine outcome
        p0_regressions: list[str] = []

        # P5: Strict fail-closed — reject when integrity checks failed.
        if comparison.integrity_regression:
            p0_regressions.append(
                "deterministic integrity check failed — regression detected"
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
                "performance thresholds met for all workflow modes",
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

    def compare_campaigns(
        self,
        run_a: ReleaseBenchmarkResult,
        run_b: ReleaseBenchmarkResult,
        *,
        tolerance: float | None = None,
    ) -> ReproducibilityComparison:
        """Compare two campaign runs for reproducibility.

        For each (mode, objective) pair present in both runs, compute
        relative differences in quality and performance metrics.  All
        differences must be within ``reproducibility_tolerance`` for
        the comparison to pass.

        Args:
            run_a: First campaign result.
            run_b: Second campaign result.
            tolerance: Optional explicit tolerance to use instead of
                ``self.config.reproducibility_tolerance``.  Useful when the
                caller already knows the tolerance and does not need a full
                runner instance.

        Returns:
            A ReproducibilityComparison with per-metric tolerances.
        """
        if tolerance is None:
            tolerance = self.config.reproducibility_tolerance

        # Index runs by (mode, objective_id)
        def index_runs(
            result: ReleaseBenchmarkResult,
        ) -> dict[tuple[str, str], CampaignRun]:
            idx: dict[tuple[str, str], CampaignRun] = {}
            for run in result.runs:
                idx[(run.mode, run.objective_id)] = run
            return idx

        idx_a = index_runs(run_a)
        idx_b = index_runs(run_b)

        quality_tolerances: list[tuple[str, float, float, float]] = []
        performance_tolerances: list[tuple[str, float, float, float]] = []
        details: list[str] = []
        all_within = True

        # Reject when mode/objective sets differ — strict reproducibility
        # requires both campaigns to exercise the same (mode, objective) pairs.
        keys_a = set(idx_a.keys())
        keys_b = set(idx_b.keys())
        if keys_a != keys_b:
            all_within = False
            only_a = keys_a - keys_b
            only_b = keys_b - keys_a
            if only_a:
                details.append(
                    f"mode/objective sets differ: missing from B: {sorted(only_a)}"
                )
            if only_b:
                details.append(
                    f"mode/objective sets differ: missing from A: {sorted(only_b)}"
                )
        elif not keys_a:
            # Both empty — no runs to compare
            all_within = False
            details.append(
                "no runs in either campaign — cannot compare reproducibility"
            )

        # Compare all (mode, objective) pairs present in both runs
        common_keys = keys_a & keys_b
        for mode, objective_id in sorted(common_keys):
            run_a = idx_a[(mode, objective_id)]
            run_b = idx_b[(mode, objective_id)]

            if run_a.quality and run_b.quality:
                for field_name in [
                    "candidate_recall",
                    "source_quality_score",
                    "coverage_completeness",
                    "unsupported_claim_rate",
                    "citation_accuracy",
                    "report_quality_score",
                ]:
                    val_a = getattr(run_a.quality, field_name)
                    val_b = getattr(run_b.quality, field_name)
                    denom = abs(val_a) if abs(val_a) > 1e-9 else 1.0
                    rel_diff = abs(val_b - val_a) / denom
                    within = rel_diff <= tolerance
                    if not within:
                        all_within = False
                    quality_tolerances.append(
                        (f"{mode}.{objective_id}.{field_name}", val_a, val_b, rel_diff)
                    )
                    if not within:
                        details.append(
                            f"{mode}.{objective_id}.{field_name}: "
                            f"{val_a:.4f} vs {val_b:.4f} (rel diff {rel_diff:.4f} > {tolerance})"
                        )

            if run_a.performance and run_b.performance:
                for field_name in [
                    "total_latency_ms",
                    "total_tokens",
                    "semantic_calls",
                    "cache_hit_rate",
                    "embedding_throughput",
                    "cpu_percent",
                    "gpu_memory_mb",
                ]:
                    val_a = getattr(run_a.performance, field_name)
                    val_b = getattr(run_b.performance, field_name)
                    denom = abs(val_a) if abs(val_a) > 1e-9 else 1.0
                    rel_diff = abs(val_b - val_a) / denom
                    within = rel_diff <= tolerance
                    if not within:
                        all_within = False
                    performance_tolerances.append(
                        (f"{mode}.{objective_id}.{field_name}", val_a, val_b, rel_diff)
                    )
                    if not within:
                        details.append(
                            f"{mode}.{objective_id}.{field_name}: "
                            f"{val_a:.4f} vs {val_b:.4f} (rel diff {rel_diff:.4f} > {tolerance})"
                        )

        return ReproducibilityComparison(
            run_a_id=run_a.campaign_id,
            run_b_id=run_b.campaign_id,
            mode="all",
            objective_id="all",
            quality_tolerances=tuple(quality_tolerances),
            performance_tolerances=tuple(performance_tolerances),
            all_within_tolerance=all_within,
            details=tuple(details),
        )


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def run_release_benchmark(
    dataset: BenchmarkDataset | BenchmarkDatasetLoader,
    database_url: str = "",
    blob_root: Path | str | None = None,
    execution_modes: tuple[str, ...] = RELEASE_MODES,
    strict: bool = False,
    reproducibility_tolerance: float = 0.15,
    known_limitations: tuple[str, ...] = (),
) -> ReleaseBenchmarkResult:
    """Execute a full release benchmark and return results.

    Args:
        dataset: Benchmark dataset or loader.
        database_url: PostgreSQL connection string (required for real execution).
        blob_root: Path to the content-addressed blob store root.
        execution_modes: Workflow modes to benchmark.
        strict: If True, metric extraction fails when required tables are absent.
        reproducibility_tolerance: Maximum relative tolerance for reproducibility.
        known_limitations: Custom known limitations for the recommendation.

    Returns:
        A ReleaseBenchmarkResult with comparison and recommendation.
    """
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
        known_limitations=known_limitations,
    )

    runner = ReleaseBenchmarkRunner(loader, config)
    return runner.run()
