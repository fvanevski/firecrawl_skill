"""Evidence, retrieval, and assessment domain contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID

from ._common import _confidence, _positive, _text, _unique
from .acquisition import IndependenceAssessment
from .research import MechanicalFailure


class SemanticStatus(str, Enum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    QUALIFIED = "qualified"
    UNSUPPORTED = "unsupported"
    UNCERTAIN = "uncertain"
    UNASSESSED = "unassessed"


class MechanicalStatus(str, Enum):
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class EvidenceRelationship(str, Enum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    QUALIFIES = "qualifies"
    CONTEXT = "context"


@dataclass(frozen=True)
class EvidenceClaim:
    claim_id: UUID
    statement: str
    semantic_status: SemanticStatus
    uncertainty: str

    def __post_init__(self):
        _text(self.statement, "evidence_claim.statement")


@dataclass(frozen=True)
class EvidencePassage:
    passage_id: UUID
    candidate_id: UUID
    snapshot_id: UUID
    chunk_id: UUID
    text: str
    source_url: str

    def __post_init__(self):
        _text(self.text, "evidence_passage.text")
        _text(self.source_url, "evidence_passage.source_url")


@dataclass(frozen=True)
class ClaimEvidenceBinding:
    binding_id: UUID
    claim_id: UUID
    passage_ids: tuple[UUID, ...]
    relationship: EvidenceRelationship
    confidence: float
    uncertainty: str
    model: str
    prompt_version: str
    schema_version: int
    input_packet_revision: int

    def __post_init__(self):
        if not self.passage_ids:
            raise ValueError("claim evidence binding requires passage IDs")
        _unique(self.passage_ids, "binding passage IDs")
        _confidence(self.confidence)
        if not self.model:
            raise ValueError("model is required")
        if not self.prompt_version:
            raise ValueError("prompt_version is required")
        if self.schema_version < 1:
            raise ValueError("schema_version must be >= 1")
        if self.input_packet_revision < 1:
            raise ValueError("input_packet_revision must be >= 1")


@dataclass(frozen=True)
class EvidenceGroup:
    group_id: UUID
    passage_ids: tuple[UUID, ...]
    rationale: str
    evaluated: bool

    def __post_init__(self):
        _unique(self.passage_ids, "group passage IDs")
        _text(self.rationale, "evidence_group.rationale")
        if not self.passage_ids and self.evaluated:
            raise ValueError(
                "empty evidence group must remain unevaluated until assessed"
            )


@dataclass(frozen=True)
class RetrievalExecution:
    execution_id: UUID
    run_id: UUID
    requested_mode: str
    executed_mode: str
    mechanical_status: MechanicalStatus
    component_health: dict[str, str]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    stage_counts: dict[str, int]
    index_fingerprint: str | None
    filters: dict[str, Any]
    skipped_stages: tuple[str, ...]
    timing: dict[str, float]
    config_identity: str

    def __post_init__(self):
        _text(self.requested_mode, "retrieval_execution.requested_mode")
        _text(self.executed_mode, "retrieval_execution.executed_mode")
        _text(self.config_identity, "retrieval_execution.config_identity")


@dataclass(frozen=True)
class RetrievalProvenance:
    retrieval_event_id: UUID
    requested_mode: str
    executed_mode: str
    mechanical_status: MechanicalStatus
    component_errors: tuple[MechanicalFailure, ...]
    selected_passage_ids: tuple[UUID, ...]

    def __post_init__(self):
        _text(self.requested_mode, "retrieval_provenance.requested_mode")
        _text(self.executed_mode, "retrieval_provenance.executed_mode")
        _unique(self.selected_passage_ids, "selected_passage_ids")
        if (
            self.mechanical_status is MechanicalStatus.SUCCEEDED
            and self.component_errors
        ):
            raise ValueError("successful retrieval cannot contain component errors")
        if (
            self.mechanical_status is not MechanicalStatus.SUCCEEDED
            and not self.component_errors
        ):
            raise ValueError("degraded or failed retrieval requires component errors")


@dataclass(frozen=True)
class EvidencePacket:
    schema_version: str
    run_id: UUID
    research_spec_id: UUID
    coverage_revision: int
    claims: tuple[EvidenceClaim, ...]
    passages: tuple[EvidencePassage, ...]
    omitted_passages: tuple[EvidencePassage, ...]
    claim_evidence_bindings: tuple[ClaimEvidenceBinding, ...]
    corroborating_groups: tuple[EvidenceGroup, ...]
    contradicting_groups: tuple[EvidenceGroup, ...]
    qualifying_groups: tuple[EvidenceGroup, ...]
    near_duplicate_groups: tuple[EvidenceGroup, ...]
    source_diversity_summary: dict[str, Any]
    freshness_summary: dict[str, Any]
    limitations: tuple[str, ...]
    unresolved_items: tuple[UUID, ...]
    independence_assessments: tuple[IndependenceAssessment, ...]
    retrieval_provenance: tuple[RetrievalProvenance, ...]

    SCHEMA_VERSION = "evidence-packet-v1"

    def __post_init__(self):
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        _positive(self.coverage_revision, "coverage_revision")
        _unique([item.claim_id for item in self.claims], "evidence claim IDs")
        all_passages = self.passages + self.omitted_passages
        _unique([item.passage_id for item in all_passages], "passage IDs")
        _unique(
            [item.binding_id for item in self.claim_evidence_bindings], "binding IDs"
        )
        groups = (
            self.corroborating_groups
            + self.contradicting_groups
            + self.qualifying_groups
            + self.near_duplicate_groups
        )
        _unique([item.group_id for item in groups], "evidence group IDs")
        _unique(
            [item.candidate_id for item in self.independence_assessments],
            "assessment candidate IDs",
        )
        claim_ids = {item.claim_id for item in self.claims}
        passage_ids = {item.passage_id for item in all_passages}
        unknown_claims = {
            item.claim_id for item in self.claim_evidence_bindings
        } - claim_ids
        unknown_passages = {
            passage
            for item in self.claim_evidence_bindings
            for passage in item.passage_ids
        } - passage_ids
        unknown_passages |= {
            passage for group in groups for passage in group.passage_ids
        } - passage_ids
        unknown_passages |= {
            passage
            for event in self.retrieval_provenance
            for passage in event.selected_passage_ids
        } - passage_ids
        if unknown_claims:
            raise ValueError(
                f"unknown evidence claim IDs: {sorted(map(str, unknown_claims))}"
            )
        if unknown_passages:
            raise ValueError(
                f"unknown passage IDs: {sorted(map(str, unknown_passages))}"
            )


# ---------------------------------------------------------------------------
# Retrieval / evidence benchmark results (Phase 6, issue #59)
# ---------------------------------------------------------------------------


class RetrievalMode(str, Enum):
    """Retrieval modes that the benchmark can exercise."""

    LEXICAL = "lexical"
    DENSE = "dense"
    FUSED = "fused"
    RERANKED = "reranked"


class DegradedMode(str, Enum):
    """Degraded modes the benchmark must represent in results."""

    LEXICAL_ONLY = "lexical_only"
    DENSE_ONLY = "dense_only"
    FUSED_ONLY = "fused_only"
    RERANKER_UNAVAILABLE = "reranker_unavailable"
    QDRANT_UNAVAILABLE = "qdrant_unavailable"
    LEXICAL_UNAVAILABLE = "lexical_unavailable"
    ALL_UNAVAILABLE = "all_unavailable"


@dataclass(frozen=True)
class RecallMeasurement:
    """Recall measurement for a single retrieval mode.

    Attributes:
        mode: The retrieval mode being measured.
        relevant_count: Total relevant items in the ground-truth set.
        retrieved_count: Items retrieved by the mode.
        relevant_retrieved: Items retrieved that are actually relevant.
        recall: relevant_retrieved / relevant_count (0.0 when relevant_count == 0).
    """

    mode: RetrievalMode
    relevant_count: int
    retrieved_count: int
    relevant_retrieved: int
    recall: float

    def __post_init__(self):
        if self.relevant_count < 0:
            raise ValueError("relevant_count must be >= 0")
        if self.retrieved_count < 0:
            raise ValueError("retrieved_count must be >= 0")
        if self.relevant_retrieved < 0:
            raise ValueError("relevant_retrieved must be >= 0")
        if self.relevant_retrieved > self.retrieved_count:
            raise ValueError("relevant_retrieved cannot exceed retrieved_count")
        if self.relevant_retrieved > self.relevant_count:
            raise ValueError("relevant_retrieved cannot exceed relevant_count")
        if self.relevant_count > 0:
            expected_recall = self.relevant_retrieved / self.relevant_count
        else:
            expected_recall = 0.0
        # Allow small floating-point tolerance
        if abs(expected_recall - self.recall) > 1e-9:
            raise ValueError(
                f"recall mismatch: computed {expected_recall}, stored {self.recall}"
            )


@dataclass(frozen=True)
class RerankerContribution:
    """Measures how much reranking changes the fused ranking.

    Attributes:
        query: The query used for retrieval.
        fused_top_k: Top-k passage IDs before reranking.
        reranked_top_k: Top-k passage IDs after reranking.
        rank_changes: Number of passages that changed rank position.
        top_k_swap: Number of passages in top-k that changed.
        mean_reciprocal_rank_before: MRR of fused top-k against ground truth.
        mean_reciprocal_rank_after: MRR of reranked top-k against ground truth.
    """

    query: str
    fused_top_k: tuple[UUID, ...]
    reranked_top_k: tuple[UUID, ...]
    rank_changes: int
    top_k_swap: int
    mean_reciprocal_rank_before: float
    mean_reciprocal_rank_after: float

    def __post_init__(self):
        if len(self.fused_top_k) != len(self.reranked_top_k):
            raise ValueError("fused_top_k and reranked_top_k must have the same length")
        if self.top_k_swap > len(self.fused_top_k):
            raise ValueError("top_k_swap cannot exceed top-k length")
        if self.rank_changes > len(self.fused_top_k):
            raise ValueError("rank_changes cannot exceed top-k length")


@dataclass(frozen=True)
class CandidateLimitMeasurement:
    """Measures recall vs. candidate count to find the knee of the curve.

    Attributes:
        candidate_limits: Tuple of candidate limits tested (ascending).
        recalls_at_limits: Recall achieved at each limit (same length).
        knee_limit: The limit where marginal recall gain drops below threshold.
        recall_gain_per_candidate: Average recall gain per additional candidate.
    """

    candidate_limits: tuple[int, ...]
    recalls_at_limits: tuple[float, ...]
    knee_limit: int
    recall_gain_per_candidate: float

    def __post_init__(self):
        if len(self.candidate_limits) < 2:
            raise ValueError("need at least two candidate limits")
        if len(self.candidate_limits) != len(self.recalls_at_limits):
            raise ValueError(
                "candidate_limits and recalls_at_limits must have the same length"
            )
        # Limits must be ascending
        for i in range(1, len(self.candidate_limits)):
            if self.candidate_limits[i] <= self.candidate_limits[i - 1]:
                raise ValueError("candidate_limits must be strictly ascending")
        # Recalls must be non-decreasing (more candidates cannot reduce recall)
        for i in range(1, len(self.recalls_at_limits)):
            if self.recalls_at_limits[i] < self.recalls_at_limits[i - 1]:
                raise ValueError("recalls_at_limits must be non-decreasing")


@dataclass(frozen=True)
class ClaimBindingQuality:
    """Measures claim-to-passage binding quality.

    Attributes:
        total_claims: Claims evaluated by the semantic model.
        bound_claims: Claims with at least one binding.
        unsupported_claims: Claims explicitly marked unsupported.
        average_confidence: Mean confidence across all bindings.
        bindings_per_claim: Average bindings per bound claim.
        single_source_claims: Claims supported by only one source.
        multi_source_claims: Claims supported by multiple sources.
    """

    total_claims: int
    bound_claims: int
    unsupported_claims: int
    average_confidence: float
    bindings_per_claim: float
    single_source_claims: int
    multi_source_claims: int

    def __post_init__(self):
        if self.bound_claims + self.unsupported_claims > self.total_claims:
            raise ValueError("bound + unsupported claims cannot exceed total claims")
        if self.single_source_claims + self.multi_source_claims > self.bound_claims:
            raise ValueError("single + multi source claims cannot exceed bound claims")


@dataclass(frozen=True)
class DuplicateGroupingResult:
    """Measures duplicate grouping quality.

    Attributes:
        total_candidates: Total candidates evaluated.
        grouped_candidates: Candidates assigned to a duplicate group.
        unassessed_candidates: Candidates that fell through all grouping criteria.
        duplicate_groups: Number of groups created.
        exact_matches: Groups formed by exact content hash.
        syndicated_matches: Groups formed by title similarity.
        canonical_matches: Groups formed by canonical URL.
        false_positive_rate: Estimated false-positive rate (0.0–1.0).
    """

    total_candidates: int
    grouped_candidates: int
    unassessed_candidates: int
    duplicate_groups: int
    exact_matches: int
    syndicated_matches: int
    canonical_matches: int
    false_positive_rate: float

    def __post_init__(self):
        if self.grouped_candidates + self.unassessed_candidates > self.total_candidates:
            raise ValueError("grouped + unassessed cannot exceed total candidates")
        if (
            self.exact_matches + self.syndicated_matches + self.canonical_matches
            > self.duplicate_groups
        ):
            raise ValueError("sum of group types cannot exceed total groups")


@dataclass(frozen=True)
class SourceIndependenceResult:
    """Measures source-independence classification quality.

    Attributes:
        total_candidates: Total candidates assessed.
        independent: Candidates assessed as independent.
        dependent: Candidates assessed as dependent.
        uncertain: Candidates assessed as uncertain.
        unassessed: Candidates with no independence assessment.
    """

    total_candidates: int
    independent: int
    dependent: int
    uncertain: int
    unassessed: int

    def __post_init__(self):
        if (
            self.independent + self.dependent + self.uncertain + self.unassessed
            > self.total_candidates
        ):
            raise ValueError("sum of independence states cannot exceed total")


@dataclass(frozen=True)
class EvidenceDensityMeasurement:
    """Measures useful-evidence density in the evidence packet.

    Attributes:
        total_passages: Total passages in the packet (including omitted).
        delivered_passages: Passages actually delivered within token budget.
        omitted_passages: Passages omitted due to token budget.
        unique_sources: Unique source URLs among delivered passages.
        source_repetition_ratio: Delivered passages / unique sources.
        average_passage_length: Mean token count of delivered passages.
    """

    total_passages: int
    delivered_passages: int
    omitted_passages: int
    unique_sources: int
    source_repetition_ratio: float
    average_passage_length: float

    def __post_init__(self):
        if self.delivered_passages + self.omitted_passages > self.total_passages:
            raise ValueError("delivered + omitted cannot exceed total passages")
        if self.unique_sources < 0:
            raise ValueError("unique_sources must be >= 0")
        if self.delivered_passages > 0 and self.unique_sources == 0:
            raise ValueError("delivered passages must have at least one source")


@dataclass(frozen=True)
class TokenBudgetMeasurement:
    """Measures deterministic token budget enforcement.

    Attributes:
        budget: Maximum tokens allowed.
        used_tokens: Tokens actually used by delivered passages.
        remaining_tokens: budget - used_tokens.
        passage_count: Number of passages delivered.
        within_budget: True if used_tokens <= budget.
    """

    budget: int
    used_tokens: int
    remaining_tokens: int
    passage_count: int
    within_budget: bool

    def __post_init__(self):
        if self.budget < 0:
            raise ValueError("budget must be >= 0")
        if self.used_tokens < 0:
            raise ValueError("used_tokens must be >= 0")
        if self.remaining_tokens != self.budget - self.used_tokens:
            raise ValueError("remaining_tokens must equal budget - used_tokens")
        if not self.within_budget and self.used_tokens > self.budget:
            raise ValueError(
                f"used_tokens ({self.used_tokens}) exceeds budget ({self.budget})"
            )


@dataclass(frozen=True)
class ProvenanceCompleteness:
    """Measures provenance completeness of evidence passages.

    Attributes:
        total_passages: Total passages in the packet.
        complete_provenance: Passages with all required provenance fields.
        missing_source: Passages missing source_url.
        missing_snapshot: Passages missing snapshot_id.
        missing_chunk: Passages missing chunk_id.
        missing_candidate: Passages missing candidate_id.
    """

    total_passages: int
    complete_provenance: int
    missing_source: int
    missing_snapshot: int
    missing_chunk: int
    missing_candidate: int

    def __post_init__(self):
        if (
            self.complete_provenance
            + max(
                self.missing_source,
                self.missing_snapshot,
                self.missing_chunk,
                self.missing_candidate,
                0,
            )
            > self.total_passages
        ):
            raise ValueError("complete + missing cannot exceed total passages")


@dataclass(frozen=True)
class DegradedModeResult:
    """Measures behavior when components are unavailable.

    Attributes:
        mode: The degraded mode tested.
        requested_mode: What the caller requested.
        executed_mode: What was actually executed.
        mechanical_status: SUCCEEDED, DEGRADED, or FAILED.
        errors: Errors encountered during degraded execution.
        warnings: Non-fatal warnings.
        recall_vs_normal: Recall in degraded mode / recall in normal mode.
        passages_delivered: Number of passages delivered in degraded mode.
        fallback_used: Whether a fallback retrieval path was used.
    """

    mode: DegradedMode
    requested_mode: str
    executed_mode: str
    mechanical_status: str  # MechanicalStatus value
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    recall_vs_normal: float
    passages_delivered: int
    fallback_used: bool

    def __post_init__(self):
        _text(self.requested_mode, "requested_mode")
        _text(self.executed_mode, "executed_mode")
        if self.recall_vs_normal < 0 or self.recall_vs_normal > 1:
            raise ValueError("recall_vs_normal must be between 0 and 1")


@dataclass(frozen=True)
class BenchmarkConfig:
    """Configurable thresholds and parameters for the benchmark.

    All thresholds are explicit configuration — no unexplained constants.
    """

    # Recall thresholds (0.0–1.0)
    min_lexical_recall: float = 0.5
    min_dense_recall: float = 0.3
    min_fused_recall: float = 0.6

    # Candidate limit knee threshold
    knee_threshold: float = 0.1

    # Token budget tolerance
    token_budget_tolerance: int = 0

    # Evidence density threshold
    min_evidence_density: float = 0.5

    # Provenance completeness threshold
    min_provenance_completeness: float = 0.95

    # Reranker contribution: minimum MRR improvement to be considered useful
    min_reranker_mrr_improvement: float = 0.0

    # Duplicate grouping: maximum false-positive rate
    max_duplicate_fp_rate: float = 0.05

    # Degraded mode: minimum recall ratio
    min_degraded_recall_ratio: float = 0.3

    # Benchmark metadata
    benchmark_version: str = "benchmark-v2"
    ground_truth_version: str = "ground-truth-v1"


@dataclass(frozen=True)
class BenchmarkResult:
    """Aggregated result of a full retrieval/evidence benchmark run.

    Attributes:
        config: The benchmark configuration used.
        lexical_recall: Recall measurement for lexical retrieval.
        dense_recall: Recall measurement for dense retrieval.
        fused_recall: Recall measurement for fused retrieval.
        reranker_contribution: Reranker impact measurement.
        candidate_limits: Candidate limit vs. recall curve.
        claim_binding_quality: Claim binding quality metrics.
        duplicate_grouping: Duplicate grouping quality metrics.
        source_independence: Source independence classification quality.
        evidence_density: Useful-evidence density measurement.
        token_budget: Token budget enforcement measurement.
        provenance_completeness: Provenance completeness measurement.
        degraded_modes: Results for each degraded mode tested.
        total_duration_ms: Wall-clock duration of the benchmark.
    """

    config: BenchmarkConfig
    lexical_recall: RecallMeasurement | None
    dense_recall: RecallMeasurement | None
    fused_recall: RecallMeasurement | None
    reranker_contribution: RerankerContribution | None
    candidate_limits: CandidateLimitMeasurement | None
    claim_binding_quality: ClaimBindingQuality | None
    duplicate_grouping: DuplicateGroupingResult | None
    source_independence: SourceIndependenceResult | None
    evidence_density: EvidenceDensityMeasurement | None
    token_budget: TokenBudgetMeasurement | None
    provenance_completeness: ProvenanceCompleteness | None
    degraded_modes: tuple[DegradedModeResult, ...]
    total_duration_ms: float

    def to_dict(self) -> dict:
        """Serialize the benchmark result to a dict."""
        from .codec import to_dict

        return to_dict(self)

    def summary(self) -> str:
        """Return a human-readable summary of the benchmark result."""
        lines = [f"Benchmark result ({self.config.benchmark_version})"]
        lines.append(f"Duration: {self.total_duration_ms:.1f}ms")

        if self.lexical_recall:
            lines.append(
                f"  Lexical recall: {self.lexical_recall.recall:.3f}"
                f" ({self.lexical_recall.relevant_retrieved}"
                f"/{self.lexical_recall.relevant_count})"
            )
        if self.dense_recall:
            lines.append(
                f"  Dense recall: {self.dense_recall.recall:.3f}"
                f" ({self.dense_recall.relevant_retrieved}"
                f"/{self.dense_recall.relevant_count})"
            )
        if self.fused_recall:
            lines.append(
                f"  Fused recall: {self.fused_recall.recall:.3f}"
                f" ({self.fused_recall.relevant_retrieved}"
                f"/{self.fused_recall.relevant_count})"
            )
        if self.reranker_contribution:
            lines.append(
                f"  Reranker MRR delta: "
                f"{self.reranker_contribution.mean_reciprocal_rank_after - self.reranker_contribution.mean_reciprocal_rank_before:+.3f}"
            )
        if self.token_budget:
            lines.append(
                f"  Token budget: {self.token_budget.used_tokens}"
                f"/{self.token_budget.budget} "
                f"({self.token_budget.passage_count} passages)"
            )
        if self.provenance_completeness:
            pct = self.provenance_completeness.complete_provenance / max(
                1, self.provenance_completeness.total_passages
            )
            lines.append(f"  Provenance completeness: {pct:.1%}")
        if self.degraded_modes:
            lines.append(f"  Degraded modes tested: {len(self.degraded_modes)}")
            for dm in self.degraded_modes:
                lines.append(
                    f"    {dm.mode.value}: recall_ratio={dm.recall_vs_normal:.3f}"
                    f" status={dm.mechanical_status}"
                )

        return "\n".join(lines)
