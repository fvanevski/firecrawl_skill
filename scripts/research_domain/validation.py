"""Cross-document referential validators for research domain contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from .codec import DomainValidationError
from .models import (
    CandidateAssessment,
    CoverageItemType,
    CoverageLedger,
    EvidencePacket,
    ResearchSpec,
    SearchPlan,
    StrategyRevisionProposal,
)


@dataclass(frozen=True)
class ValidationContext:
    research_spec: ResearchSpec | None = None
    coverage_ledger: CoverageLedger | None = None
    run_id: UUID | None = None
    current_run_revision: int | None = None
    current_coverage_revision: int | None = None
    candidate_ids: frozenset[UUID] = field(default_factory=frozenset)
    snapshot_ids: frozenset[UUID] = field(default_factory=frozenset)
    passage_ids: frozenset[UUID] = field(default_factory=frozenset)


def _reject_unknown(values, known, name):
    unknown = set(values) - set(known)
    if unknown:
        raise DomainValidationError(f"unknown {name}: {sorted(map(str, unknown))}")


def _spec_ids(spec: ResearchSpec):
    return {
        CoverageItemType.QUESTION: {item.question_id for item in spec.questions},
        CoverageItemType.CLAIM: {item.claim_id for item in spec.claims_to_validate},
        CoverageItemType.SOURCE_REQUIREMENT: {item.requirement_id for item in spec.required_source_classes},
        CoverageItemType.FRESHNESS_REQUIREMENT: {item.requirement_id for item in spec.freshness_requirements},
        CoverageItemType.CORROBORATION_REQUIREMENT: {item.requirement_id for item in spec.corroboration_requirements},
        CoverageItemType.CONTRADICTION_REQUIREMENT: {item.requirement_id for item in spec.contradiction_requirements},
    }


def _validate_search_targets(queries, spec):
    question_ids = {item.question_id for item in spec.questions}
    claim_ids = {item.claim_id for item in spec.claims_to_validate}
    for query in queries:
        _reject_unknown(query.target_question_ids, question_ids, "question IDs")
        _reject_unknown(query.target_claim_ids, claim_ids, "claim IDs")


def validate_references(model, context: ValidationContext):
    spec = context.research_spec
    if isinstance(model, SearchPlan):
        if spec is None:
            raise DomainValidationError("SearchPlan validation requires ResearchSpec")
        if model.research_spec_id != spec.research_spec_id:
            raise DomainValidationError("SearchPlan references another ResearchSpec")
        _validate_search_targets(model.queries, spec)
    elif isinstance(model, CandidateAssessment):
        if context.candidate_ids:
            _reject_unknown([model.candidate_id], context.candidate_ids, "candidate IDs")
        if spec is None:
            raise DomainValidationError("CandidateAssessment validation requires ResearchSpec")
        _validate_search_targets([model], spec)
    elif isinstance(model, CoverageLedger):
        if context.run_id and model.run_id != context.run_id:
            raise DomainValidationError("CoverageLedger references another run")
        if spec is None:
            raise DomainValidationError("CoverageLedger validation requires ResearchSpec")
        known_subjects = _spec_ids(spec)
        for item in model.items:
            try:
                subject = UUID(item.subject_id)
            except ValueError as exc:
                raise DomainValidationError(f"invalid coverage subject ID: {item.subject_id}") from exc
            _reject_unknown([subject], known_subjects[item.item_type], f"{item.item_type.value} subject IDs")
            if context.candidate_ids:
                _reject_unknown(item.candidate_ids, context.candidate_ids, "candidate IDs")
            if context.snapshot_ids:
                _reject_unknown(item.snapshot_ids, context.snapshot_ids, "snapshot IDs")
            if context.passage_ids:
                _reject_unknown(item.passage_ids, context.passage_ids, "passage IDs")
    elif isinstance(model, StrategyRevisionProposal):
        ledger = context.coverage_ledger
        if ledger is None:
            raise DomainValidationError("StrategyRevisionProposal validation requires CoverageLedger")
        if context.current_run_revision is not None and model.run_revision != context.current_run_revision:
            raise DomainValidationError("stale run revision")
        if model.coverage_revision != ledger.revision:
            raise DomainValidationError("stale coverage revision")
        _reject_unknown(
            model.target_coverage_item_ids,
            {item.coverage_item_id for item in ledger.items},
            "coverage item IDs",
        )
        if context.candidate_ids:
            _reject_unknown(model.proposed_candidate_ids, context.candidate_ids, "candidate IDs")
        if spec and model.proposed_queries:
            _validate_search_targets(model.proposed_queries, spec)
    elif isinstance(model, EvidencePacket):
        if context.run_id and model.run_id != context.run_id:
            raise DomainValidationError("EvidencePacket references another run")
        if spec and model.research_spec_id != spec.research_spec_id:
            raise DomainValidationError("EvidencePacket references another ResearchSpec")
        if spec:
            _reject_unknown(
                [item.claim_id for item in model.claims],
                {item.claim_id for item in spec.claims_to_validate},
                "claim IDs",
            )
        expected_revision = context.current_coverage_revision
        if expected_revision is None and context.coverage_ledger:
            expected_revision = context.coverage_ledger.revision
        if expected_revision is not None and model.coverage_revision != expected_revision:
            raise DomainValidationError("stale coverage revision")
        if context.candidate_ids:
            _reject_unknown(
                [item.candidate_id for item in model.passages],
                context.candidate_ids,
                "candidate IDs",
            )
        if context.snapshot_ids:
            _reject_unknown(
                [item.snapshot_id for item in model.passages],
                context.snapshot_ids,
                "snapshot IDs",
            )
        if context.coverage_ledger:
            _reject_unknown(
                model.unresolved_items,
                {item.coverage_item_id for item in context.coverage_ledger.items},
                "coverage item IDs",
            )
    return model
