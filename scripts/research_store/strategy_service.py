"""Strategy-revision proposal and deterministic authorization service.

This module implements the ``StrategyRevisionService`` which persists
strategy-revision proposals and records deterministic authorization
decisions.  It is the bridge between the semantic authority (which
proposes actions) and the deterministic policy engine (which decides
whether those actions are valid and permitted).

Key invariants:

* Every proposal is persisted before any adaptive action executes.
* Proposals are validated against run revision, coverage revision,
  scope, budget, and novelty constraints.
* Accepted proposals are recorded with their decision ID.
* Rejected proposals remain auditable with rejection reasons.
* Stale proposals (against old revisions) are rejected.
* No adaptive query, scrape, or retrieval action executes without
  a recorded authorization decision.

This service is intentionally thin — it contains no semantic
assessment logic.  Semantic judgments flow through
SemanticCallService; this service only persists deterministic
observations and authorization decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID, uuid4

from research_domain.models import (
    RejectionReason,
    ScopeExpansionRationale,
    StrategyDecision,
    StrategyRevisionDecision,
    StrategyRevisionProposal,
)

from budget_policy import BudgetPolicy, BudgetSnapshot

from .strategy_validator import (
    StrategyRevisionValidator,
    ValidationResult,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Service exceptions
# ---------------------------------------------------------------------------


class StrategyServiceError(ValueError):
    """A strategy service operation violated a schema or policy invariant."""


class ProposalNotFoundError(StrategyServiceError):
    """A strategy proposal was not found for the given run and ID."""


class DecisionNotFoundError(StrategyServiceError):
    """A strategy decision was not found for the given run and ID."""


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposalSummary:
    """Compact summary of a persisted strategy proposal."""

    proposal_id: UUID
    run_id: UUID
    run_revision: int
    coverage_revision: int
    decision_type: str
    target_coverage_item_ids: tuple[UUID, ...]
    proposed_query_count: int
    proposed_candidate_count: int
    proposed_retrieval_count: int
    rationale: str
    confidence: float
    created_at: datetime

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ProposalSummary":
        return cls(
            proposal_id=UUID(str(value["proposal_id"])),
            run_id=UUID(str(value["run_id"])),
            run_revision=value["run_revision"],
            coverage_revision=value["coverage_revision"],
            decision_type=value["decision_type"],
            target_coverage_item_ids=tuple(
                UUID(str(iid)) for iid in value.get("target_coverage_item_ids", [])
            ),
            proposed_query_count=value.get("proposed_query_count", 0),
            proposed_candidate_count=value.get("proposed_candidate_count", 0),
            proposed_retrieval_count=value.get("proposed_retrieval_count", 0),
            rationale=value.get("rationale", ""),
            confidence=value.get("confidence", 0.0),
            created_at=value["created_at"],
        )


@dataclass(frozen=True)
class DecisionSummary:
    """Compact summary of a persisted strategy decision."""

    decision_id: UUID
    proposal_id: UUID
    run_id: UUID
    run_revision: int
    coverage_revision: int
    outcome: str
    rejection_reasons: tuple[str, ...]
    policy_version: str
    authorized_by: str
    created_at: datetime

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "DecisionSummary":
        return cls(
            decision_id=UUID(str(value["decision_id"])),
            proposal_id=UUID(str(value["proposal_id"])),
            run_id=UUID(str(value["run_id"])),
            run_revision=value["run_revision"],
            coverage_revision=value["coverage_revision"],
            outcome=value["outcome"],
            rejection_reasons=tuple(value.get("rejection_reasons", [])),
            policy_version=value.get("policy_version", ""),
            authorized_by=value.get("authorized_by", ""),
            created_at=value["created_at"],
        )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class StrategyRevisionService:
    """Persist strategy-revision proposals and record authorization decisions.

    Public API:

    * ``create_proposal`` — persist a new strategy-revision proposal.
    * ``get_proposal`` — retrieve a persisted proposal.
    * ``list_proposals`` — list proposals for a run.
    * ``authorize`` — validate and record an authorization decision.
    * ``get_decision`` — retrieve a persisted decision.
    * ``list_decisions`` — list decisions for a run or proposal.
    * ``validate_proposal`` — run validation without persisting.
    """

    def __init__(
        self,
        uow_factory: Callable,
        budget_policy: BudgetPolicy | None = None,
    ) -> None:
        self.uow_factory = uow_factory
        self.budget_policy = budget_policy or BudgetPolicy.load()
        self.validator = StrategyRevisionValidator(self.budget_policy)

    # ------------------------------------------------------------------
    # Proposal lifecycle
    # ------------------------------------------------------------------

    def create_proposal(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int,
        decision_type: str,
        target_coverage_item_ids: list[UUID],
        proposed_queries: list[dict[str, Any]],
        proposed_candidate_ids: list[UUID] | None = None,
        proposed_retrieval_queries: list[str] | None = None,
        expected_contribution: str = "",
        estimated_cost: dict[str, int] | None = None,
        rationale: str = "",
        confidence: float = 0.0,
        *,
        idempotency_key: str | None = None,
        actor_type: str = "semantic_authority",
        actor_identifier: str | None = None,
    ) -> StrategyRevisionProposal:
        """Persist a new strategy-revision proposal.

        This does NOT authorize the proposal — it only records it.
        Authorization is handled by ``authorize``.
        """
        if not run_id:
            raise StrategyServiceError("run_id is required")
        if run_revision < 1:
            raise StrategyServiceError("run_revision must be >= 1")
        if coverage_revision < 1:
            raise StrategyServiceError("coverage_revision must be >= 1")
        if not decision_type:
            raise StrategyServiceError("decision_type is required")
        if not target_coverage_item_ids:
            raise StrategyServiceError("target_coverage_item_ids is required")

        # Generate idempotency key before creating the proposal
        idempotency_key = (
            idempotency_key or f"proposal:{run_id}:{decision_type}:{coverage_revision}"
        )
        estimated_cost = estimated_cost or {}
        proposed_candidate_ids = proposed_candidate_ids or []
        proposed_retrieval_queries = proposed_retrieval_queries or []
        # Provide defaults for required text fields when empty
        effective_contribution = expected_contribution or "No contribution specified"
        effective_rationale = rationale or "No rationale provided"

        with self.uow_factory() as uow:
            # Check if proposal already exists by idempotency key
            existing = uow.strategy_revisions.get_proposal_by_idempotency(
                run_id, idempotency_key
            )
            if existing is not None:
                return self._mapping_to_proposal(existing)

            proposal_id = uuid4()
            proposal = StrategyRevisionProposal(
                schema_version="strategy-revision-v1",
                proposal_id=proposal_id,
                run_revision=run_revision,
                coverage_revision=coverage_revision,
                decision=StrategyDecision(decision_type),
                target_coverage_item_ids=tuple(target_coverage_item_ids),
                proposed_queries=tuple(self._make_query(q) for q in proposed_queries),
                proposed_candidate_ids=tuple(proposed_candidate_ids),
                proposed_retrieval_queries=tuple(proposed_retrieval_queries),
                expected_contribution=effective_contribution,
                estimated_cost=estimated_cost,
                rationale=effective_rationale,
                confidence=confidence,
            )

            uow.strategy_revisions.record_proposal(
                run_id=run_id,
                proposal_id=proposal_id,
                run_revision=run_revision,
                coverage_revision=coverage_revision,
                decision_type=decision_type,
                target_coverage_item_ids=[str(iid) for iid in target_coverage_item_ids],
                proposed_queries=proposed_queries,
                proposed_candidate_ids=[str(cid) for cid in proposed_candidate_ids],
                proposed_retrieval_queries=proposed_retrieval_queries,
                expected_contribution=effective_contribution,
                estimated_cost=estimated_cost,
                rationale=effective_rationale,
                confidence=confidence,
                idempotency_key=idempotency_key,
                actor_type=actor_type,
                actor_identifier=actor_identifier,
            )

        return proposal

    def get_proposal(self, run_id: UUID, proposal_id: UUID) -> StrategyRevisionProposal:
        """Retrieve a persisted strategy proposal."""
        with self.uow_factory() as uow:
            result = uow.strategy_revisions.get_proposal(run_id, proposal_id)
        if result is None:
            raise ProposalNotFoundError(
                f"proposal {proposal_id} not found for run {run_id}"
            )
        return self._mapping_to_proposal(result)

    def list_proposals(
        self,
        run_id: UUID,
        *,
        run_revision: int | None = None,
        coverage_revision: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ProposalSummary]:
        """List proposals for a run, optionally filtered by revision."""
        with self.uow_factory() as uow:
            rows = uow.strategy_revisions.list_proposals(
                run_id,
                run_revision=run_revision,
                coverage_revision=coverage_revision,
                limit=limit,
                offset=offset,
            )
        return [ProposalSummary.from_mapping(r) for r in rows]

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------

    def authorize(
        self,
        run_id: UUID,
        proposal_id: UUID,
        *,
        current_run_revision: int,
        current_coverage_revision: int,
        run_state: str,
        is_terminal: bool = False,
        existing_query_signatures: list[str] | None = None,
        existing_candidate_ids: set[UUID] | None = None,
        existing_retrieval_queries: set[str] | None = None,
        budget_snapshot: BudgetSnapshot | None = None,
        scope_expansion: ScopeExpansionRationale | None = None,
        run_exists: bool | None = None,
        coverage_items_exist: bool | None = None,
        actor_type: str = "deterministic_policy",
        actor_identifier: str | None = None,
        idempotency_key: str | None = None,
    ) -> StrategyRevisionDecision:
        """Validate and record an authorization decision for a proposal.

        This method:
        1. Retrieves the proposal.
        2. Runs deterministic validation (optionally with scope expansion).
        3. Records an accepted or rejected decision.

        Returns the ``StrategyRevisionDecision``.
        """
        proposal = self.get_proposal(run_id, proposal_id)

        # Extract proposal fields for validation
        decision_type = proposal.decision.value
        target_item_ids = list(proposal.target_coverage_item_ids)
        # proposed_queries are stored as dicts, not SearchQuery objects
        proposed_queries = [
            q if isinstance(q, dict) else {"query": q.query, "facet": q.facet}
            for q in proposal.proposed_queries
        ]
        proposed_candidates = list(proposal.proposed_candidate_ids)
        proposed_retrieval = list(proposal.proposed_retrieval_queries)
        estimated_cost = proposal.estimated_cost

        # Validate
        # TODO (#26): Populate run_exists and coverage_items_exist from the
        # repository when integrated into ResearchRunService. Currently the
        # caller must supply these parameters; issue #26 will wire the
        # strategy-revision service into the coverage-led state machine.
        result = self.validator.validate(
            run_id=run_id,
            run_revision=proposal.run_revision,
            coverage_revision=proposal.coverage_revision,
            decision_type=decision_type,
            target_coverage_item_ids=target_item_ids,
            proposed_queries=proposed_queries,
            proposed_candidate_ids=proposed_candidates,
            proposed_retrieval_queries=proposed_retrieval,
            estimated_cost=estimated_cost,
            rationale=proposal.rationale,
            scope_expansion=scope_expansion,
            current_run_revision=current_run_revision,
            current_coverage_revision=current_coverage_revision,
            run_state=run_state,
            existing_query_signatures=existing_query_signatures,
            existing_candidate_ids=existing_candidate_ids,
            existing_retrieval_queries=existing_retrieval_queries,
            budget_snapshot=budget_snapshot,
            run_exists=run_exists,
            coverage_items_exist=coverage_items_exist,
            is_terminal=is_terminal,
        )

        decision_id = uuid4()
        now = utcnow()

        if result.valid:
            outcome = "accepted"
            rejection_reasons = []
        else:
            outcome = "rejected"
            rejection_reasons = [r.value for r in result.rejection_reasons]

        with self.uow_factory() as uow:
            uow.strategy_revisions.record_decision(
                run_id=run_id,
                decision_id=decision_id,
                proposal_id=proposal_id,
                run_revision=proposal.run_revision,
                coverage_revision=proposal.coverage_revision,
                outcome=outcome,
                rejection_reasons=rejection_reasons,
                policy_version=self.budget_policy.policy_version,
                scope_expansion_type=(
                    scope_expansion.expansion_type.value
                    if scope_expansion is not None
                    else None
                ),
                scope_expansion_rationale=(
                    scope_expansion.rationale if scope_expansion is not None else None
                ),
                scope_expansion_approved=(
                    scope_expansion.approved if scope_expansion is not None else None
                ),
                authorized_by=actor_type,
                idempotency_key=idempotency_key or f"decision:{decision_id}",
                actor_type=actor_type,
                actor_identifier=actor_identifier,
            )

        return StrategyRevisionDecision(
            decision_id=decision_id,
            proposal_id=proposal_id,
            run_id=run_id,
            run_revision=proposal.run_revision,
            coverage_revision=proposal.coverage_revision,
            outcome=outcome,
            rejection_reasons=tuple(RejectionReason(r) for r in rejection_reasons),
            policy_version=self.budget_policy.policy_version,
            scope_expansion=result.scope_expansion,
            authorized_by=actor_type,
            created_at=now,
        )

    def get_decision(self, run_id: UUID, decision_id: UUID) -> StrategyRevisionDecision:
        """Retrieve a persisted authorization decision."""
        with self.uow_factory() as uow:
            result = uow.strategy_revisions.get_decision(run_id, decision_id)
        if result is None:
            raise DecisionNotFoundError(
                f"decision {decision_id} not found for run {run_id}"
            )
        return self._mapping_to_decision(result)

    def list_decisions(
        self,
        run_id: UUID,
        *,
        proposal_id: UUID | None = None,
        outcome: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DecisionSummary]:
        """List decisions for a run or proposal, optionally filtered."""
        with self.uow_factory() as uow:
            rows = uow.strategy_revisions.list_decisions(
                run_id,
                proposal_id=proposal_id,
                outcome=outcome,
                limit=limit,
                offset=offset,
            )
        return [DecisionSummary.from_mapping(r) for r in rows]

    # ------------------------------------------------------------------
    # Validation-only (no persistence)
    # ------------------------------------------------------------------

    def validate_proposal(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int,
        decision_type: str,
        target_coverage_item_ids: list[UUID],
        proposed_queries: list[dict[str, Any]],
        proposed_candidate_ids: list[UUID] | None = None,
        proposed_retrieval_queries: list[str] | None = None,
        estimated_cost: dict[str, int] | None = None,
        rationale: str = "",
        scope_expansion: ScopeExpansionRationale | None = None,
        *,
        current_run_revision: int,
        current_coverage_revision: int,
        run_state: str,
        is_terminal: bool = False,
        budget_snapshot: BudgetSnapshot | None = None,
        existing_query_signatures: list[str] | None = None,
        existing_candidate_ids: set[UUID] | None = None,
        existing_retrieval_queries: set[str] | None = None,
        run_exists: bool | None = None,
        coverage_items_exist: bool | None = None,
    ) -> ValidationResult:
        """Run deterministic validation without persisting.

        Returns a ``ValidationResult`` that can be used to decide
        whether to persist and authorize the proposal.
        """
        return self.validator.validate(
            run_id=run_id,
            run_revision=run_revision,
            coverage_revision=coverage_revision,
            decision_type=decision_type,
            target_coverage_item_ids=target_coverage_item_ids,
            proposed_queries=proposed_queries,
            proposed_candidate_ids=proposed_candidate_ids or [],
            proposed_retrieval_queries=proposed_retrieval_queries or [],
            estimated_cost=estimated_cost or {},
            rationale=rationale,
            scope_expansion=scope_expansion,
            current_run_revision=current_run_revision,
            current_coverage_revision=current_coverage_revision,
            run_state=run_state,
            existing_query_signatures=existing_query_signatures,
            existing_candidate_ids=existing_candidate_ids,
            existing_retrieval_queries=existing_retrieval_queries,
            budget_snapshot=budget_snapshot,
            run_exists=run_exists,
            coverage_items_exist=coverage_items_exist,
            is_terminal=is_terminal,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_query(self, query: dict[str, Any]) -> Any:
        """Convert a query dict to a SearchQuery-like object.

        In the full implementation this would create a proper
        ``SearchQuery`` domain model. For now we return the dict
        as-is since the domain model is not strictly enforced here.
        """
        return query

    def _mapping_to_proposal(self, mapping: dict[str, Any]) -> StrategyRevisionProposal:
        """Convert a repository mapping to a domain model."""
        target_ids = [
            UUID(str(iid)) for iid in mapping.get("target_coverage_item_ids", [])
        ]
        queries = mapping.get("proposed_queries", [])
        candidate_ids = [
            UUID(str(cid)) for cid in mapping.get("proposed_candidate_ids", [])
        ]
        retrieval_queries = mapping.get("proposed_retrieval_queries", [])
        contribution = (
            mapping.get("expected_contribution", "") or "No contribution specified"
        )
        rationale = mapping.get("rationale", "") or "No rationale provided"

        return StrategyRevisionProposal(
            schema_version="strategy-revision-v1",
            proposal_id=UUID(str(mapping["proposal_id"])),
            run_revision=mapping["run_revision"],
            coverage_revision=mapping["coverage_revision"],
            decision=StrategyDecision(mapping["decision_type"]),
            target_coverage_item_ids=tuple(target_ids),
            proposed_queries=tuple(queries),
            proposed_candidate_ids=tuple(candidate_ids),
            proposed_retrieval_queries=tuple(retrieval_queries),
            expected_contribution=contribution,
            estimated_cost=mapping.get("estimated_cost", {}),
            rationale=rationale,
            confidence=mapping.get("confidence", 0.0),
        )

    def _mapping_to_decision(self, mapping: dict[str, Any]) -> StrategyRevisionDecision:
        """Convert a repository mapping to a domain model."""
        rejection_reasons = [
            RejectionReason(r) for r in mapping.get("rejection_reasons", [])
        ]
        # Reconstruct scope_expansion from stored fields when present
        scope_expansion = None
        outcome = mapping.get("outcome", "")
        if outcome == "accepted":
            expansion_type = mapping.get("scope_expansion_type")
            expansion_rationale = mapping.get("scope_expansion_rationale")
            expansion_approved = mapping.get("scope_expansion_approved")
            if (
                expansion_type is not None
                and expansion_rationale is not None
                and expansion_approved is not None
            ):
                try:
                    from research_domain.models import ScopeExpansionType

                    scope_expansion = ScopeExpansionRationale(
                        expansion_type=ScopeExpansionType(expansion_type),
                        rationale=expansion_rationale,
                        approved=expansion_approved,
                    )
                except ValueError:
                    # Invalid expansion_type — treat as no expansion
                    pass
        return StrategyRevisionDecision(
            decision_id=UUID(str(mapping["decision_id"])),
            proposal_id=UUID(str(mapping["proposal_id"])),
            run_id=UUID(str(mapping["run_id"])),
            run_revision=mapping["run_revision"],
            coverage_revision=mapping["coverage_revision"],
            outcome=mapping["outcome"],
            rejection_reasons=tuple(rejection_reasons),
            policy_version=mapping.get("policy_version", ""),
            scope_expansion=scope_expansion,
            authorized_by=mapping.get("authorized_by", ""),
            created_at=mapping["created_at"],
        )


__all__ = [
    "StrategyRevisionService",
    "StrategyServiceError",
    "ProposalNotFoundError",
    "DecisionNotFoundError",
    "ProposalSummary",
    "DecisionSummary",
]
