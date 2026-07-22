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

    def __post_init__(self):
        if not self.passage_ids:
            raise ValueError("claim evidence binding requires passage IDs")
        _unique(self.passage_ids, "binding passage IDs")
        _confidence(self.confidence)


@dataclass(frozen=True)
class EvidenceGroup:
    group_id: UUID
    passage_ids: tuple[UUID, ...]
    rationale: str
    evaluated: bool

    def __post_init__(self):
        _unique(self.passage_ids, "group passage IDs")
        _text(self.rationale, "evidence_group.rationale")
        if not self.passage_ids and not self.evaluated:
            raise ValueError("empty evidence group must record evaluated absence")


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
    claim_evidence_bindings: tuple[ClaimEvidenceBinding, ...]
    corroborating_groups: tuple[EvidenceGroup, ...]
    contradicting_groups: tuple[EvidenceGroup, ...]
    qualifying_groups: tuple[EvidenceGroup, ...]
    near_duplicate_groups: tuple[EvidenceGroup, ...]
    source_diversity_summary: dict[str, Any]
    freshness_summary: dict[str, Any]
    limitations: tuple[str, ...]
    unresolved_items: tuple[UUID, ...]
    retrieval_provenance: tuple[RetrievalProvenance, ...]

    SCHEMA_VERSION = "evidence-packet-v1"

    def __post_init__(self):
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        _positive(self.coverage_revision, "coverage_revision")
        _unique([item.claim_id for item in self.claims], "evidence claim IDs")
        _unique([item.passage_id for item in self.passages], "passage IDs")
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
        claim_ids = {item.claim_id for item in self.claims}
        passage_ids = {item.passage_id for item in self.passages}
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
    created_at: Any  # datetime

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
