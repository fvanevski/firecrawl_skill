"""Canonical typed contracts for the research workflow.

These models contain no transport, persistence, or orchestration behavior.
They describe validated proposals and projections that deterministic services
may later persist or act upon.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty")


def _confidence(value: float, name: str = "confidence") -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 1
    ):
        raise ValueError(f"{name} must be between 0 and 1")


def _positive(value: int, name: str, *, allow_zero: bool = False) -> None:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")


def _unique(values, name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must not contain duplicates")


def _temporal(value: str | None, name: str):
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO-8601 date or datetime") from exc


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ExecutionMode(str, Enum):
    AGENT_LED = "agent_led"
    AUTONOMOUS_LOCAL = "autonomous_local"
    DETERMINISTIC_DEBUG = "deterministic_debug"


class Relevance(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNRELATED = "unrelated"
    UNCERTAIN = "uncertain"


class SourceRole(str, Enum):
    PRIMARY = "primary"
    CONTROLLING = "controlling"
    AUTHORITATIVE_SECONDARY = "authoritative_secondary"
    INDEPENDENT_SECONDARY = "independent_secondary"
    CONTEXT_ONLY = "context_only"
    UNSUITABLE = "unsuitable"
    UNCERTAIN = "uncertain"


class ExtractionRecommendation(str, Enum):
    SCRAPE = "scrape"
    METADATA_ONLY = "metadata_only"
    DEFER = "defer"
    REJECT = "reject"


class IndependenceStatus(str, Enum):
    INDEPENDENT = "independent"
    DEPENDENT = "dependent"
    UNCERTAIN = "uncertain"
    UNASSESSED = "unassessed"


class CoverageItemType(str, Enum):
    QUESTION = "question"
    CLAIM = "claim"
    SOURCE_REQUIREMENT = "source_requirement"
    FRESHNESS_REQUIREMENT = "freshness_requirement"
    CORROBORATION_REQUIREMENT = "corroboration_requirement"
    CONTRADICTION_REQUIREMENT = "contradiction_requirement"


class CoverageStatus(str, Enum):
    MISSING = "missing"
    CANDIDATE_IDENTIFIED = "candidate_identified"
    ACQUIRED = "acquired"
    PARTIALLY_SUPPORTED = "partially_supported"
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    QUALIFIED = "qualified"
    SATISFIED = "satisfied"
    BLOCKED = "blocked"
    WAIVED = "waived"
    UNASSESSED = "unassessed"


class FreshnessStatus(str, Enum):
    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    UNCERTAIN = "uncertain"
    NOT_APPLICABLE = "not_applicable"


class OverallCoverageStatus(str, Enum):
    INSUFFICIENT = "insufficient"
    PARTIAL = "partial"
    SUFFICIENT = "sufficient"
    BLOCKED = "blocked"
    UNASSESSED = "unassessed"


class StrategyDecision(str, Enum):
    SEARCH = "search"
    SCRAPE = "scrape"
    RETRIEVE = "retrieve"
    SYNTHESIZE = "synthesize"
    STOP_PARTIAL = "stop_partial"
    STOP_FAILED = "stop_failed"


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


class FailureStatus(str, Enum):
    DEGRADED = "degraded"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class EvidenceRelationship(str, Enum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    QUALIFIES = "qualifies"
    CONTEXT = "context"


@dataclass(frozen=True)
class ResearchQuestion:
    question_id: UUID
    text: str

    def __post_init__(self):
        _text(self.text, "question.text")


@dataclass(frozen=True)
class ResearchClaim:
    claim_id: UUID
    statement: str

    def __post_init__(self):
        _text(self.statement, "claim.statement")


@dataclass(frozen=True)
class TimeWindow:
    start: str | None
    end: str | None
    description: str
    uncertainty: str

    def __post_init__(self):
        if not self.start and not self.end and not self.description.strip():
            raise ValueError("time_window needs a bound or description")
        start = _temporal(self.start, "time_window.start")
        end = _temporal(self.end, "time_window.end")
        if start and end and start > end:
            raise ValueError("time_window.start must not be after time_window.end")


@dataclass(frozen=True)
class FreshnessRequirement:
    requirement_id: UUID
    description: str
    max_age_days: int | None

    def __post_init__(self):
        _text(self.description, "freshness_requirement.description")
        if self.max_age_days is not None:
            _positive(self.max_age_days, "max_age_days", allow_zero=True)


@dataclass(frozen=True)
class SourceRequirement:
    requirement_id: UUID
    source_class: str
    minimum_count: int

    def __post_init__(self):
        _text(self.source_class, "source_requirement.source_class")
        _positive(self.minimum_count, "minimum_count")


@dataclass(frozen=True)
class EvidenceRequirement:
    requirement_id: UUID
    description: str
    required_independent_source_count: int

    def __post_init__(self):
        _text(self.description, "evidence_requirement.description")
        _positive(
            self.required_independent_source_count,
            "required_independent_source_count",
            allow_zero=True,
        )


@dataclass(frozen=True)
class StructuredDataRequirement:
    requirement_id: UUID
    description: str
    required_fields: tuple[str, ...]

    def __post_init__(self):
        _text(self.description, "structured_data_requirement.description")
        if not self.required_fields:
            raise ValueError("structured data requirement needs required_fields")
        _unique(self.required_fields, "required_fields")


@dataclass(frozen=True)
class CompletionCriterion:
    criterion_id: UUID
    description: str
    mandatory: bool

    def __post_init__(self):
        _text(self.description, "completion_criterion.description")


@dataclass(frozen=True)
class ResearchSpec:
    schema_version: str
    research_spec_id: UUID
    objective: str
    research_archetype: str
    risk_level: RiskLevel
    execution_mode: ExecutionMode
    questions: tuple[ResearchQuestion, ...]
    claims_to_validate: tuple[ResearchClaim, ...]
    entities: tuple[str, ...]
    jurisdictions: tuple[str, ...]
    time_window: TimeWindow
    freshness_requirements: tuple[FreshnessRequirement, ...]
    required_source_classes: tuple[SourceRequirement, ...]
    corroboration_requirements: tuple[EvidenceRequirement, ...]
    contradiction_requirements: tuple[EvidenceRequirement, ...]
    excluded_interpretations: tuple[str, ...]
    structured_data_requirements: tuple[StructuredDataRequirement, ...]
    completion_criteria: tuple[CompletionCriterion, ...]
    user_constraints: tuple[str, ...]
    ambiguities: tuple[str, ...]
    assumptions: tuple[str, ...]

    SCHEMA_VERSION = "research-spec-v1"

    def __post_init__(self):
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        _text(self.objective, "objective")
        _text(self.research_archetype, "research_archetype")
        if not self.questions:
            raise ValueError("ResearchSpec requires at least one question")
        if not self.completion_criteria:
            raise ValueError("ResearchSpec requires bounded completion criteria")
        for values, name in (
            ([item.question_id for item in self.questions], "question IDs"),
            ([item.claim_id for item in self.claims_to_validate], "claim IDs"),
            (
                [item.requirement_id for item in self.freshness_requirements],
                "freshness requirement IDs",
            ),
            (
                [item.requirement_id for item in self.required_source_classes],
                "source requirement IDs",
            ),
            (
                [item.requirement_id for item in self.corroboration_requirements],
                "corroboration requirement IDs",
            ),
            (
                [item.requirement_id for item in self.contradiction_requirements],
                "contradiction requirement IDs",
            ),
            (
                [item.requirement_id for item in self.structured_data_requirements],
                "structured requirement IDs",
            ),
            (
                [item.criterion_id for item in self.completion_criteria],
                "completion criterion IDs",
            ),
        ):
            _unique(values, name)


@dataclass(frozen=True)
class SearchQuery:
    query_id: UUID
    query: str
    facet: str
    target_question_ids: tuple[UUID, ...]
    target_claim_ids: tuple[UUID, ...]
    intended_source_classes: tuple[str, ...]
    expected_organizations: tuple[str, ...]
    freshness_requirement: TimeWindow
    expected_contribution: str
    domain_restrictions: tuple[str, ...]
    negative_terms: tuple[str, ...]
    priority: int

    def __post_init__(self):
        _text(self.query, "search_query.query")
        _text(self.facet, "search_query.facet")
        _text(self.expected_contribution, "search_query.expected_contribution")
        if not self.target_question_ids and not self.target_claim_ids:
            raise ValueError("search query must target a question or claim")
        _unique(self.target_question_ids, "target_question_ids")
        _unique(self.target_claim_ids, "target_claim_ids")
        _unique(self.domain_restrictions, "domain_restrictions")
        _positive(self.priority, "priority", allow_zero=True)


@dataclass(frozen=True)
class SearchPlan:
    schema_version: str
    research_spec_id: UUID
    revision: int
    queries: tuple[SearchQuery, ...]

    SCHEMA_VERSION = "search-plan-v1"

    def __post_init__(self):
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        _positive(self.revision, "revision")
        if not self.queries:
            raise ValueError("SearchPlan requires at least one query")
        _unique([item.query_id for item in self.queries], "query IDs")
        normalized = [" ".join(item.query.split()).casefold() for item in self.queries]
        _unique(normalized, "normalized queries")


@dataclass(frozen=True)
class FreshnessAssessment:
    status: FreshnessStatus
    rationale: str

    def __post_init__(self):
        _text(self.rationale, "freshness_assessment.rationale")


@dataclass(frozen=True)
class IndependenceAssessment:
    candidate_id: UUID
    status: IndependenceStatus
    rationale: str

    def __post_init__(self):
        _text(self.rationale, "independence_assessment.rationale")


@dataclass(frozen=True)
class CandidateAssessment:
    schema_version: str
    candidate_id: UUID
    relevance: Relevance
    source_role: SourceRole
    target_question_ids: tuple[UUID, ...]
    target_claim_ids: tuple[UUID, ...]
    freshness_assessment: FreshnessAssessment
    independence_assessment: IndependenceAssessment
    extraction_recommendation: ExtractionRecommendation
    priority: int
    rationale: str
    confidence: float
    uncertainty: str

    SCHEMA_VERSION = "candidate-assessment-v1"

    def __post_init__(self):
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        _positive(self.priority, "priority", allow_zero=True)
        if self.priority > 100:
            raise ValueError("priority must be <= 100")
        _text(self.rationale, "rationale")
        _confidence(self.confidence)
        _unique(self.target_question_ids, "target_question_ids")
        _unique(self.target_claim_ids, "target_claim_ids")
        if not self.target_question_ids and not self.target_claim_ids:
            raise ValueError("candidate assessment must target a question or claim")


@dataclass(frozen=True)
class MechanicalFailure:
    failure_id: UUID
    component: str
    error_class: str
    message: str
    status: FailureStatus
    retryable: bool

    def __post_init__(self):
        _text(self.component, "mechanical_failure.component")
        _text(self.error_class, "mechanical_failure.error_class")
        _text(self.message, "mechanical_failure.message")


@dataclass(frozen=True)
class CoverageItem:
    coverage_item_id: UUID
    item_type: CoverageItemType
    subject_id: str
    status: CoverageStatus
    candidate_ids: tuple[UUID, ...]
    snapshot_ids: tuple[UUID, ...]
    passage_ids: tuple[UUID, ...]
    independent_source_count: int
    required_independent_source_count: int
    authority_classes_present: tuple[str, ...]
    freshness_status: FreshnessStatus
    remaining_gap: str
    confidence: float
    mechanical_failure_ids: tuple[UUID, ...]

    def __post_init__(self):
        _text(self.subject_id, "coverage_item.subject_id")
        _positive(
            self.independent_source_count, "independent_source_count", allow_zero=True
        )
        _positive(
            self.required_independent_source_count,
            "required_independent_source_count",
            allow_zero=True,
        )
        _confidence(self.confidence)
        _unique(self.candidate_ids, "candidate_ids")
        _unique(self.snapshot_ids, "snapshot_ids")
        _unique(self.passage_ids, "passage_ids")
        _unique(self.mechanical_failure_ids, "mechanical_failure_ids")


@dataclass(frozen=True)
class CoverageLedger:
    schema_version: str
    run_id: UUID
    revision: int
    items: tuple[CoverageItem, ...]
    overall_status: OverallCoverageStatus
    mechanical_failures: tuple[MechanicalFailure, ...]

    SCHEMA_VERSION = "coverage-ledger-v1"

    def __post_init__(self):
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        _positive(self.revision, "revision")
        _unique([item.coverage_item_id for item in self.items], "coverage item IDs")
        _unique(
            [item.failure_id for item in self.mechanical_failures],
            "mechanical failure IDs",
        )
        known = {item.failure_id for item in self.mechanical_failures}
        referenced = {
            item for coverage in self.items for item in coverage.mechanical_failure_ids
        }
        if referenced - known:
            raise ValueError(
                f"unknown mechanical failure IDs: {sorted(map(str, referenced - known))}"
            )


@dataclass(frozen=True)
class StrategyRevisionProposal:
    schema_version: str
    proposal_id: UUID
    run_revision: int
    coverage_revision: int
    decision: StrategyDecision
    target_coverage_item_ids: tuple[UUID, ...]
    proposed_queries: tuple[SearchQuery, ...]
    proposed_candidate_ids: tuple[UUID, ...]
    proposed_retrieval_queries: tuple[str, ...]
    expected_contribution: str
    estimated_cost: dict[str, int]
    rationale: str
    confidence: float

    SCHEMA_VERSION = "strategy-revision-v1"

    def __post_init__(self):
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        _positive(self.run_revision, "run_revision")
        _positive(self.coverage_revision, "coverage_revision")
        if not self.target_coverage_item_ids:
            raise ValueError("strategy proposal must target coverage items")
        _unique(self.target_coverage_item_ids, "target_coverage_item_ids")
        _unique(self.proposed_candidate_ids, "proposed_candidate_ids")
        _text(self.expected_contribution, "expected_contribution")
        _text(self.rationale, "rationale")
        _confidence(self.confidence)


# ---------------------------------------------------------------------------
# Strategy revision authorization
# ---------------------------------------------------------------------------


class RejectionReason(str, Enum):
    """Taxonomy of deterministic rejection reasons for strategy proposals."""

    STALE_COVERAGE_REVISION = "stale_coverage_revision"
    STALE_RUN_REVISION = "stale_run_revision"
    UNKNOWN_COVERAGE_ITEM = "unknown_coverage_item"
    UNKNOWN_RUN = "unknown_run"
    BUDGET_EXCEEDED = "budget_exceeded"
    SCOPE_EXPANDED = "scope_expanded"
    SCOPE_EXPANSION_UNJUSTIFIED = "scope_expansion_unjustified"
    DUPLICATE_ACTION = "duplicate_action"
    MISSING_RATIONALE = "missing_rationale"
    MISSING_TARGET_ITEMS = "missing_target_items"
    TERMINAL_RUN_STATE = "terminal_run_state"
    UNKNOWN_DECISION_TYPE = "unknown_decision_type"


class ScopeExpansionType(str, Enum):
    """Types of scope expansion detected in a proposal."""

    NEW_ENTITIES = "new_entities"
    NEW_JURISDICTIONS = "new_jurisdictions"
    NEW_TIME_WINDOWS = "new_time_windows"
    NEW_SOURCE_CLASSES = "new_source_classes"
    NEW_ARCHETYPE = "new_archetype"
    BROADENED_QUERY_TERMS = "broadened_query_terms"


@dataclass(frozen=True)
class ScopeExpansionRationale:
    """Explicit rationale required when a proposal expands scope.

    Scope expansion is permitted only when the rationale is provided
    and passes deterministic policy checks.
    """

    expansion_type: ScopeExpansionType
    rationale: str
    approved: bool

    def __post_init__(self):
        _text(self.rationale, "scope_expansion_rationale.rationale")


@dataclass(frozen=True)
class StrategyRevisionDecision:
    """Deterministic authorization decision on a strategy proposal.

    This record captures whether the proposal was accepted or rejected,
    the rejection reason taxonomy (if rejected), and the deterministic
    policy version that made the decision.
    """

    decision_id: UUID
    proposal_id: UUID
    run_id: UUID
    run_revision: int
    coverage_revision: int
    outcome: str  # "accepted" | "rejected"
    rejection_reasons: tuple[RejectionReason, ...]
    policy_version: str
    scope_expansion: ScopeExpansionRationale | None
    authorized_by: str  # "deterministic_policy" | "operator"
    created_at: Any  # datetime

    def __post_init__(self):
        if self.outcome not in ("accepted", "rejected"):
            raise ValueError("outcome must be 'accepted' or 'rejected'")
        if not self.rejection_reasons and self.outcome == "rejected":
            raise ValueError("rejected decisions must include rejection reasons")
        if self.outcome == "accepted" and self.rejection_reasons:
            raise ValueError("accepted decisions must not include rejection reasons")
        if self.scope_expansion and self.outcome == "rejected":
            raise ValueError("scope_expansion is only recorded for accepted proposals")
        _text(self.policy_version, "decision.policy_version")
        _text(self.authorized_by, "decision.authorized_by")


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
            raise ValueError(
                "fused_top_k and reranked_top_k must have the same length"
            )
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
                raise ValueError(
                    "recalls_at_limits must be non-decreasing"
                )


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
            raise ValueError(
                "bound + unsupported claims cannot exceed total claims"
            )
        if self.single_source_claims + self.multi_source_claims > self.bound_claims:
            raise ValueError(
                "single + multi source claims cannot exceed bound claims"
            )


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
            raise ValueError(
                "grouped + unassessed cannot exceed total candidates"
            )
        if self.exact_matches + self.syndicated_matches + self.canonical_matches > self.duplicate_groups:
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
        if self.independent + self.dependent + self.uncertain + self.unassessed > self.total_candidates:
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
            raise ValueError(
                "delivered + omitted cannot exceed total passages"
            )
        if self.unique_sources < 0:
            raise ValueError("unique_sources must be >= 0")
        if self.delivered_passages > 0 and self.unique_sources == 0:
            raise ValueError(
                "delivered passages must have at least one source"
            )


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
        if self.complete_provenance + max(
            self.missing_source, self.missing_snapshot,
            self.missing_chunk, self.missing_candidate, 0
        ) > self.total_passages:
            raise ValueError(
                "complete + missing cannot exceed total passages"
            )


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
    benchmark_version: str = "benchmark-v1"
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
        from research_store.evidence import _to_dict
        return _to_dict(self)

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
            lines.append(
                f"  Provenance completeness: {pct:.1%}"
            )
        if self.degraded_modes:
            lines.append(f"  Degraded modes tested: {len(self.degraded_modes)}")
            for dm in self.degraded_modes:
                lines.append(
                    f"    {dm.mode.value}: recall_ratio={dm.recall_vs_normal:.3f}"
                    f" status={dm.mechanical_status}"
                )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Terminal decision (Phase 3 / FR-012)
# ---------------------------------------------------------------------------


class NoProgressSignal(str, Enum):
    """Deterministic signals that the adaptive loop is not making progress."""

    NO_NEW_CANDIDATES = "no_new_candidates"
    NO_NEW_ASSETS = "no_new_assets"
    NO_CHANGED_COVERAGE = "no_changed_coverage"
    REPEATED_EQUIVALENT_PROPOSALS = "repeated_equivalent_proposals"
    REPEATED_EXTRACTION_FAILURES = "repeated_extraction_failures"
    REPEATED_RETRIEVAL = "repeated_retrieval"
    COST_BUDGET_EXHAUSTED = "cost_budget_exhausted"
    WALL_CLOCK_EXHAUSTED = "wall_clock_exhausted"
    UNSATISFIABLE_SOURCE = "unsatisfiable_source"


class TerminalDecisionOutcome(str, Enum):
    """Deterministic terminal outcomes for a research run.

    CANCELLED is reserved for external/operator-triggered termination
    (e.g., manual cancellation via CLI or API) and is not produced by
    any internal signal condition in TerminalDecisionPolicy.
    """

    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TerminalDecision:
    """Deterministic terminal decision produced by TerminalDecisionPolicy.

    This is a pure data contract — it carries no orchestration or
    persistence logic.  The orchestrator is responsible for translating
    the decision into a run-state transition via ResearchRunService.
    """

    schema_version: str
    decision_id: UUID
    run_id: UUID
    run_revision: int
    coverage_revision: int
    outcome: TerminalDecisionOutcome
    no_progress_signals: tuple[NoProgressSignal, ...]
    unresolved_gap: str
    policy_version: str
    created_at: datetime

    SCHEMA_VERSION = "terminal-decision-v1"
    POLICY_VERSION = "terminal-decision-policy-v1"

    def __post_init__(self):
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if self.policy_version != self.POLICY_VERSION:
            raise ValueError(f"unsupported policy_version: {self.policy_version}")
        _unique(
            [s.value for s in self.no_progress_signals],
            "no_progress_signals",
        )
        _text(self.unresolved_gap, "terminal_decision.unresolved_gap")


CANONICAL_MODELS = (
    ResearchSpec,
    SearchPlan,
    CandidateAssessment,
    CoverageLedger,
    StrategyRevisionProposal,
    EvidencePacket,
    TerminalDecision,
)
