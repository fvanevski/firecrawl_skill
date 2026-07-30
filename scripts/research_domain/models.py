"""Canonical typed contracts for the research workflow.

These models contain no transport, persistence, or orchestration behavior.
They describe validated proposals and projections that deterministic services
may later persist or act upon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar
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
            lines.append(f"  Provenance completeness: {pct:.1%}")
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


# ---------------------------------------------------------------------------
# Agent-led handoff payload (Phase 7, issue #62)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HandoffPayload:
    """Bounded, self-contained handoff for a host agent.

    The payload gives the host agent everything needed to draft a report
    without scanning scratch files or triggering redundant semantic calls.
    Every citation resolves to a passage inside the evidence packet.

    Attributes:
        schema_version: Always ``"handoff-payload-v1"``.
        run_id: The research run this handoff belongs to.
        research_spec: Serialized ``ResearchSpec`` with all requirements.
        coverage_ledger: Serialized ``CoverageLedger`` with current status.
        evidence_packet: Serialized ``EvidencePacket`` with claims, passages,
            bindings, and groups.
        evidence_packet_revision: The packet revision this payload reflects.
        coverage_revision: The coverage revision the ledger reflects.
        limitations: Explicit limitations and degraded states.
        unresolved_items: Coverage-item IDs that remain unresolved.
        outline: Optional structured outline for the report (``None`` when
            no outline was produced).
        citation_ready: Bounded, citation-ready subset of the packet
            (claims, passages, and bindings) suitable for host-agent
            synthesis.
        token_limits: Effective token limits derived from the budget policy
            (``None`` when limits are not applicable).
        created_at: Timestamp when the payload was constructed.
    """

    schema_version: str
    run_id: UUID
    research_spec: dict[str, Any]
    coverage_ledger: dict[str, Any]
    evidence_packet: dict[str, Any]
    evidence_packet_revision: int
    coverage_revision: int
    limitations: tuple[str, ...]
    unresolved_items: tuple[UUID, ...]
    outline: tuple[str, ...] | None
    citation_ready: dict[str, Any]
    token_limits: dict[str, int] | None
    created_at: datetime

    SCHEMA_VERSION = "handoff-payload-v1"

    def __post_init__(self) -> None:
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        # evidence_packet_revision == 0 is allowed for degraded payloads
        # where no evidence packet exists.
        if self.evidence_packet_revision < 0:
            raise ValueError("evidence_packet_revision must be >= 0")
        if self.coverage_revision < 0:
            raise ValueError("coverage_revision must be >= 0")

    def to_dict(self) -> dict[str, Any]:
        """Serialize the payload to a JSON-compatible dictionary."""
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "run_id": str(self.run_id),
            "research_spec": self.research_spec,
            "coverage_ledger": self.coverage_ledger,
            "evidence_packet": self.evidence_packet,
            "evidence_packet_revision": self.evidence_packet_revision,
            "coverage_revision": self.coverage_revision,
            "limitations": list(self.limitations),
            "unresolved_items": [str(uid) for uid in self.unresolved_items],
            "outline": (list(self.outline) if self.outline is not None else None),
            "citation_ready": self.citation_ready,
            "token_limits": self.token_limits,
            "created_at": (
                self.created_at.isoformat()
                if hasattr(self.created_at, "isoformat")
                else str(self.created_at)
            ),
        }
        return result


# ---------------------------------------------------------------------------
# Release benchmark campaign (Phase 7, issue #67)
# ---------------------------------------------------------------------------


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
    """A known source referenced in a benchmark objective.

    Attributes:
        schema_version: Always ``"benchmark-source-v1"``.
        file_path: Path to the source file (relative to skill root).
        relevance: Whether this source is expected to be relevant.
        role: Role of this source in the benchmark ("relevant", "distractor").
    """

    schema_version: str
    file_path: str
    relevance: bool
    role: str  # "relevant" | "distractor"

    SCHEMA_VERSION = "benchmark-source-v1"

    def __post_init__(self) -> None:
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if self.role not in ("relevant", "distractor"):
            raise ValueError(
                f"role must be 'relevant' or 'distractor', got: {self.role}"
            )


@dataclass(frozen=True)
class BenchmarkObjective:
    """A single research objective in the benchmark dataset.

    Attributes:
        schema_version: ``"benchmark-objective-v2"`` for the executable
            release objective contract. Version 1 remains readable.
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
    SCHEMA_VERSIONS = ("benchmark-objective-v1", "benchmark-objective-v2")
    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = (
        "search_queries",
        "search_query_expected_sources",
        "ground_truth_answers",
        "citation_support_labels",
    )

    def __post_init__(self) -> None:
        if self.schema_version not in self.SCHEMA_VERSIONS:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        _text(self.id, "benchmark_objective.id")
        _text(self.title, "benchmark_objective.title")
        _text(self.objective, "benchmark_objective.objective")
        if not self.questions:
            raise ValueError("benchmark_objective.questions must not be empty")
        if self.schema_version == self.SCHEMA_VERSION:
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
        schema_version: ``"benchmark-dataset-v2"`` for the executable release
            contract. Version 1 remains readable.
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
    SCHEMA_VERSIONS = ("benchmark-dataset-v1", "benchmark-dataset-v2")

    def __post_init__(self) -> None:
        if self.schema_version not in self.SCHEMA_VERSIONS:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
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


# ---------------------------------------------------------------------------
# Phase 7, issue #143 — Run-scoped performance telemetry models
# ---------------------------------------------------------------------------


class TelemetryStatus(str, Enum):
    """Status vocabulary for telemetry availability.

    Measured zero (a real instrument returned 0) is distinct from
    unavailable (the instrument is absent or failed).
    """

    MEASURED = "measured"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"
    STALE = "stale"
    INVALID = "invalid"


class CacheEventType(str, Enum):
    """Types of cache events recorded for a benchmark run."""

    LOOKUP = "lookup"
    HIT = "hit"
    MISS = "miss"
    INVALIDATION = "invalidation"
    REUSE = "reuse"


class EndpointType(str, Enum):
    """Types of model endpoints tracked for telemetry."""

    GENERATIVE = "generative"
    EMBEDDING = "embedding"
    RERANKING = "reranking"


@dataclass(frozen=True)
class TokenAccounting:
    """Token counts for a semantic call, measured or tokenizer-derived.

    Attributes:
        schema_version: One of ``"token-accounting-v1"``.
        prompt_tokens: Prompt token count from endpoint response (None if
            unavailable).
        completion_tokens: Completion token count from endpoint response
            (None if unavailable).
        total_tokens: Total token count from endpoint response (None if
            unavailable). May differ from prompt+completion when the
            endpoint reports a separate total.
        tokenizer_prompt_tokens: Token count derived via tokenizer when
            endpoint usage is absent.
        tokenizer_completion_tokens: Token count derived via tokenizer when
            endpoint usage is absent.
        tokenizer_total_tokens: Total derived from tokenizer.
        source: ``"endpoint"`` when endpoint response provides usage,
            ``"tokenizer"`` when derived from the stored request/response,
            ``"unavailable"`` when neither is available.
        metric_version: Version of the token-accounting method.
    """

    schema_version: str = "token-accounting-v1"
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    tokenizer_prompt_tokens: int | None = None
    tokenizer_completion_tokens: int | None = None
    tokenizer_total_tokens: int | None = None
    source: str = "unavailable"
    metric_version: str = "token-accounting-v1"

    SCHEMA_VERSION = "token-accounting-v1"
    SCHEMA_VERSIONS = ("token-accounting-v1",)

    def __post_init__(self) -> None:
        if self.schema_version != "token-accounting-v1":
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if self.source not in (
            "endpoint",
            "tokenizer",
            "not_invoked",
            "unavailable",
        ):
            raise ValueError(
                "source must be endpoint, tokenizer, not_invoked, or unavailable; "
                f"got: {self.source}"
            )

        # If endpoint source, at least one field must be present.
        if self.source == "endpoint" and (
            self.prompt_tokens is None
            and self.completion_tokens is None
            and self.total_tokens is None
        ):
            raise ValueError(
                "endpoint source requires at least one of prompt_tokens, "
                "completion_tokens, or total_tokens"
            )

        # If tokenizer source, at least one field must be present.
        if self.source == "tokenizer" and (
            self.tokenizer_prompt_tokens is None
            and self.tokenizer_completion_tokens is None
            and self.tokenizer_total_tokens is None
        ):
            raise ValueError(
                "tokenizer source requires at least one of tokenizer_prompt_tokens, "
                "tokenizer_completion_tokens, or tokenizer_total_tokens"
            )

    @property
    def total(self) -> int | None:
        """Return the total token count from the preferred source."""
        if self.source == "endpoint" and self.total_tokens is not None:
            return self.total_tokens
        if self.source == "tokenizer" and self.tokenizer_total_tokens is not None:
            return self.tokenizer_total_tokens
        if (
            self.source == "endpoint"
            and self.prompt_tokens is not None
            and self.completion_tokens is not None
        ):
            return self.prompt_tokens + self.completion_tokens
        if (
            self.source == "tokenizer"
            and self.tokenizer_prompt_tokens is not None
            and self.tokenizer_completion_tokens is not None
        ):
            return self.tokenizer_prompt_tokens + self.tokenizer_completion_tokens
        return None

    @property
    def status(self) -> TelemetryStatus:
        """Return the availability status of this token accounting."""
        if self.source == "endpoint":
            return TelemetryStatus.MEASURED
        if self.source == "tokenizer":
            return TelemetryStatus.MEASURED
        return TelemetryStatus.UNAVAILABLE


@dataclass(frozen=True)
class CacheEvent:
    """A single cache event scoped to a run and semantic stage.

    Attributes:
        schema_version: Always ``"cache-event-v1"``.
        run_id: UUID of the research run.
        stage: Semantic stage (outline, binding, draft, citation_pass).
        event_type: Type of cache event.
        key_hash: SHA-256 key hash of the cache entry.
        model_fingerprint: Model fingerprint for the cache entry.
        hit: Whether this lookup resulted in a cache hit (for LOOKUP events).
        metric_version: Version of the cache-event schema.
    """

    schema_version: str = "cache-event-v1"
    run_id: str = ""
    stage: str = ""
    event_type: str = ""
    key_hash: str = ""
    model_fingerprint: str = ""
    hit: bool | None = None
    metric_version: str = "cache-event-v1"

    SCHEMA_VERSION = "cache-event-v1"
    SCHEMA_VERSIONS = ("cache-event-v1",)

    def __post_init__(self) -> None:
        if self.schema_version != "cache-event-v1":
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if self.event_type not in ("lookup", "hit", "miss", "invalidation", "reuse"):
            raise ValueError(
                f"event_type must be lookup, hit, miss, invalidation, or reuse; got: {self.event_type}"
            )
        if self.event_type in ("hit", "miss", "lookup") and self.hit is None:
            raise ValueError(f"hit field is required for event_type={self.event_type}")


@dataclass(frozen=True)
class EmbeddingThroughputRecord:
    """Record of embedding throughput for a run or stage.

    Attributes:
        schema_version: Always ``"embedding-throughput-v1"``.
        run_id: UUID of the research run.
        stage: Stage name (e.g. "embedding", "indexing").
        batch_count: Number of batch requests made.
        vector_count: Number of vectors produced (excluding failures).
        failed_count: Number of embedding requests that failed.
        total_texts: Total input texts processed.
        elapsed_seconds: Wall-clock time spent in embedding calls.
        endpoint_url: The embedding endpoint URL.
        endpoint_model: The model name used.
        dimension: Vector dimension produced.
        metric_version: Version of the embedding-throughput schema.
    """

    schema_version: str = "embedding-throughput-v1"
    run_id: str = ""
    stage: str = ""
    batch_count: int = 0
    vector_count: int = 0
    failed_count: int = 0
    total_texts: int = 0
    elapsed_seconds: float = 0.0
    endpoint_url: str = ""
    endpoint_model: str = ""
    dimension: int | None = None
    metric_version: str = "embedding-throughput-v1"

    SCHEMA_VERSION = "embedding-throughput-v1"
    SCHEMA_VERSIONS = ("embedding-throughput-v1",)

    def __post_init__(self) -> None:
        if self.schema_version != "embedding-throughput-v1":
            raise ValueError(f"unsupported schema_version: {self.schema_version}")

    @property
    def throughput(self) -> float:
        """Texts per second, or 0.0 when no elapsed time."""
        if self.elapsed_seconds <= 0:
            return 0.0
        return round(self.total_texts / self.elapsed_seconds, 3)

    @property
    def status(self) -> TelemetryStatus:
        """Return availability status."""
        if self.batch_count > 0 and self.elapsed_seconds > 0:
            return TelemetryStatus.MEASURED
        if self.batch_count == 0 and self.elapsed_seconds == 0:
            return TelemetryStatus.UNAVAILABLE
        return TelemetryStatus.PARTIAL


@dataclass(frozen=True)
class ResourceSample:
    """A single CPU or GPU resource sample over the run window.

    Attributes:
        schema_version: Always ``"resource-sample-v1"``.
        run_id: UUID of the research run.
        device_type: ``"cpu"`` or ``"gpu"``.
        device_index: Hardware device index (0-based).
        device_uuid: Hardware UUID, when available.
        sample_type: Type of measurement (e.g. ``"cpu_percent"``, ``"gpu_memory_used_mb"``).
        value: The measured value. Nullable — samples with ``status != 'measured'``
            may have ``value=None``.
        sample_at: ISO-8601 timestamp of the sample.
        collector: Library used (e.g. ``"psutil"``, ``"pynvml"``).
        collector_version: Version of the collector library.
        sample_number: Sequential sample number within the run.
        metric_version: Version of the resource-sample schema.
        status: Availability status of this sample.
        failure_reason: Explicit reason when status is not ``"measured"``.
        window_start: ISO-8601 timestamp when the workload window began.
        window_end: ISO-8601 timestamp when the workload window ended.
        sampling_interval_seconds: Interval between samples in the workload window.
    """

    schema_version: str = "resource-sample-v1"
    run_id: str = ""
    device_type: str = ""
    device_index: int = 0
    device_uuid: str = ""
    sample_type: str = ""
    value: float | None = None
    sample_at: str = ""
    collector: str = ""
    collector_version: str = ""
    sample_number: int = 0
    metric_version: str = "resource-sample-v1"
    status: str = "measured"
    failure_reason: str = ""
    window_start: str = ""
    window_end: str = ""
    sampling_interval_seconds: float = 0.0

    SCHEMA_VERSION = "resource-sample-v1"
    SCHEMA_VERSIONS = ("resource-sample-v1",)

    def __post_init__(self) -> None:
        if self.schema_version != "resource-sample-v1":
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if self.device_type not in ("cpu", "gpu"):
            raise ValueError(f"device_type must be cpu or gpu; got: {self.device_type}")
        if self.status not in (
            "measured",
            "unavailable",
            "partial",
            "stale",
            "invalid",
        ):
            raise ValueError(f"invalid status: {self.status}")


@dataclass(frozen=True)
class EndpointUsageRecord:
    """Record of endpoint usage for a semantic call.

    Captures actual token usage from the endpoint response, or falls back
    to tokenizer-based counting when the endpoint does not provide usage.

    Attributes:
        schema_version: Always ``"endpoint-usage-v1"``.
        run_id: UUID of the research run.
        call_id: UUID of the semantic call.
        endpoint_type: Type of endpoint (generative, embedding, reranking).
        provider: Provider name (e.g. ``"openai-compatible"``).
        model: Model name.
        model_revision: Model revision string.
        prompt_tokens: From endpoint response or tokenizer.
        completion_tokens: From endpoint response or tokenizer.
        total_tokens: From endpoint response or tokenizer.
        source: ``"endpoint"`` or ``"tokenizer"``.
        metric_version: Version of the endpoint-usage schema.
    """

    schema_version: str = "endpoint-usage-v1"
    run_id: str = ""
    call_id: str = ""
    endpoint_type: str = ""
    provider: str = ""
    model: str = ""
    model_revision: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    source: str = "unavailable"
    metric_version: str = "endpoint-usage-v1"

    SCHEMA_VERSION = "endpoint-usage-v1"
    SCHEMA_VERSIONS = ("endpoint-usage-v1",)

    def __post_init__(self) -> None:
        if self.schema_version != "endpoint-usage-v1":
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if self.source not in (
            "endpoint",
            "tokenizer",
            "not_invoked",
            "unavailable",
        ):
            raise ValueError(
                "source must be endpoint, tokenizer, not_invoked, or unavailable; "
                f"got: {self.source}"
            )
        if self.endpoint_type and self.endpoint_type not in (
            "generative",
            "embedding",
            "reranking",
        ):
            raise ValueError(f"invalid endpoint_type: {self.endpoint_type}")


@dataclass(frozen=True)
class PerformanceTelemetrySummary:
    """Aggregated run-scoped performance telemetry summary.

    Attributes:
        schema_version: Always ``"performance-telemetry-summary-v1"``.
        run_id: UUID of the research run.
        total_tokens: Sum of all token counts for the run.
        token_source: Source of the token counts
            (``"endpoint"``, ``"tokenizer"``, ``"unavailable"``).
        semantic_calls: Total semantic calls for the run.
        cache_lookups: Total cache lookups for the run.
        cache_hits: Total cache hits for the run.
        cache_misses: Total cache misses for the run.
        cache_hit_rate: Cache hit rate (0.0–1.0), or None when unavailable.
        embedding_batch_count: Total embedding batches.
        embedding_vector_count: Total embedding vectors produced.
        embedding_elapsed_seconds: Total embedding time.
        embedding_throughput: Texts per second, or 0.0 when unavailable.
        cpu_samples: Number of CPU samples collected.
        cpu_mean_percent: Mean CPU usage, or None when no samples.
        cpu_max_percent: Maximum CPU usage, or None when no samples.
        gpu_samples: Number of GPU samples collected.
        gpu_mean_memory_mb: Mean GPU memory, or None when no samples.
        gpu_max_memory_mb: Maximum GPU memory, or None when no samples.
        gpu_unavailable: Whether GPU telemetry is unavailable.
            GPU is optional — unavailability does not cause strict_pass
            to be False. This accommodates CPU-only environments where
            NVML is absent or the GPU is reserved for the local LLM agent.
        strict_pass: Whether all required metrics are measured (not estimated).
            GPU unavailability does not affect strict_pass.
        metric_version: Version of the summary schema.
    """

    schema_version: str = "performance-telemetry-summary-v1"
    run_id: str = ""
    total_tokens: int = 0
    token_source: str = "unavailable"
    semantic_calls: int = 0
    cache_lookups: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    cache_hit_rate: float | None = None
    embedding_batch_count: int = 0
    embedding_vector_count: int = 0
    embedding_elapsed_seconds: float = 0.0
    embedding_throughput: float = 0.0
    cpu_samples: int = 0
    cpu_mean_percent: float | None = None
    cpu_max_percent: float | None = None
    gpu_samples: int = 0
    gpu_mean_memory_mb: float | None = None
    gpu_max_memory_mb: float | None = None
    gpu_unavailable: bool = True
    strict_pass: bool = True
    metric_version: str = "performance-telemetry-summary-v1"

    SCHEMA_VERSION = "performance-telemetry-summary-v1"
    SCHEMA_VERSIONS = ("performance-telemetry-summary-v1",)

    def __post_init__(self) -> None:
        if self.schema_version != "performance-telemetry-summary-v1":
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if self.token_source not in (
            "endpoint",
            "tokenizer",
            "not_invoked",
            "unavailable",
        ):
            raise ValueError(
                "token_source must be endpoint, tokenizer, not_invoked, or unavailable; "
                f"got: {self.token_source}"
            )
        if self.cache_hit_rate is not None and not (0.0 <= self.cache_hit_rate <= 1.0):
            raise ValueError("cache_hit_rate must be between 0.0 and 1.0")


CANONICAL_MODELS = (
    ResearchSpec,
    SearchPlan,
    CandidateAssessment,
    CoverageLedger,
    StrategyRevisionProposal,
    EvidencePacket,
    TerminalDecision,
    HandoffPayload,
    # Phase 7, issue #67 — Release benchmark campaign
    BenchmarkDataset,
    BenchmarkObjective,
    BenchmarkSource,
    QualityMeasurement,
    PerformanceMeasurement,
    DeterministicIntegrityCheck,
    WorkflowRunResult,
    WorkflowComparison,
    ReleaseRecommendation,
    # Phase 7, issue #143 — Run-scoped performance telemetry
    TokenAccounting,
    CacheEvent,
    EmbeddingThroughputRecord,
    ResourceSample,
    EndpointUsageRecord,
    PerformanceTelemetrySummary,
)
