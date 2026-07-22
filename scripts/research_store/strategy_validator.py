"""Deterministic validation for strategy-revision proposals.

This module implements the policy engine that decides whether a
``StrategyRevisionProposal`` is valid and permitted.  The semantic
authority may propose new queries, scrapes, retrieval actions, synthesis,
partial stop, or failed stop — but deterministic code decides whether
the proposal is valid and authorized.

Key invariants:

* Every proposal must reference an existing run and coverage revision.
* Target coverage items must exist and belong to the run.
* Stale coverage or run revisions are rejected.
* Budget proposals are checked against the effective hard limits.
* Scope expansion requires explicit rationale and policy approval.
* Duplicate actions (same query + target items) are rejected.
* Terminal run states block new proposals.
* Unknown decision types are rejected.

This module contains no semantic assessment logic.  It only validates
deterministic observations against schema, reference, revision, scope,
and budget constraints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from research_domain.models import (
    RejectionReason,
    ScopeExpansionRationale,
)

from budget_policy import BudgetDecision, BudgetPolicy, BudgetSnapshot


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class StrategyValidationError(ValueError):
    """A strategy proposal violated a schema or policy invariant."""


class StaleRunRevisionError(StrategyValidationError):
    """The proposal references an older run lifecycle revision."""


class StaleCoverageRevisionError(StrategyValidationError):
    """The proposal references an older coverage revision."""


class UnknownCoverageItemError(StrategyValidationError):
    """A target coverage item does not exist for the run."""


class UnknownRunError(StrategyValidationError):
    """The run ID does not exist or is not in a proposal-accepting state."""


class DuplicateActionError(StrategyValidationError):
    """A proposal duplicates an already-authorized action."""


class BudgetExceededError(StrategyValidationError):
    """The proposal exceeds the effective hard budget limits."""


class ScopeExpansionError(StrategyValidationError):
    """Scope expansion was detected but rationale is missing or unjustified."""


class TerminalRunStateError(StrategyValidationError):
    """The run is in a terminal state and cannot accept new proposals."""


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of deterministic proposal validation."""

    valid: bool
    rejection_reasons: tuple[RejectionReason, ...]
    scope_expansion: ScopeExpansionRationale | None
    budget_decision: BudgetDecision | None

    @classmethod
    def accepted(
        cls,
        scope_expansion: ScopeExpansionRationale | None = None,
        budget_decision: BudgetDecision | None = None,
    ) -> "ValidationResult":
        return cls(
            valid=True,
            rejection_reasons=(),
            scope_expansion=scope_expansion,
            budget_decision=budget_decision,
        )

    @classmethod
    def rejected(
        cls,
        *reasons: RejectionReason,
        scope_expansion: ScopeExpansionRationale | None = None,
        budget_decision: BudgetDecision | None = None,
    ) -> "ValidationResult":
        return cls(
            valid=False,
            rejection_reasons=tuple(reasons),
            scope_expansion=scope_expansion,
            budget_decision=budget_decision,
        )


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class StrategyRevisionValidator:
    """Deterministic policy checks for strategy-revision proposals.

    Public API:

    * ``validate`` — run all validation checks and return a ValidationResult.
    * ``validate_queries`` — check proposed queries for novelty and scope.
    * ``validate_budget`` — check estimated cost against effective caps.
    * ``validate_scope`` — detect and validate scope expansion.
    * ``validate_revision`` — check run and coverage revision staleness.
    """

    def __init__(
        self,
        budget_policy: BudgetPolicy,
        *,
        require_rationale: bool = True,
        require_target_items: bool = True,
    ) -> None:
        self.budget_policy = budget_policy
        self.require_rationale = require_rationale
        self.require_target_items = require_target_items

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def validate(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int,
        decision_type: str,
        target_coverage_item_ids: list[UUID],
        proposed_queries: list[dict[str, Any]],
        proposed_candidate_ids: list[UUID],
        proposed_retrieval_queries: list[str],
        estimated_cost: dict[str, int],
        rationale: str | None,
        scope_expansion: ScopeExpansionRationale | None,
        *,
        current_run_revision: int,
        current_coverage_revision: int,
        run_state: str,
        existing_query_signatures: list[str] | None = None,
        existing_candidate_ids: set[UUID] | None = None,
        existing_retrieval_queries: set[str] | None = None,
        budget_snapshot: BudgetSnapshot | None = None,
        spec: Mapping[str, Any] | None = None,
        is_terminal: bool = False,
    ) -> ValidationResult:
        """Run all deterministic validation checks.

        Returns a ``ValidationResult`` that is either accepted or rejected
        with a taxonomy of rejection reasons.
        """
        reasons: list[RejectionReason] = []

        # 1. Terminal state check
        if is_terminal:
            reasons.append(RejectionReason.TERMINAL_RUN_STATE)

        # 2. Unknown decision type
        if not self._is_valid_decision_type(decision_type):
            reasons.append(RejectionReason.UNKNOWN_DECISION_TYPE)

        # 3. Missing target items
        if self.require_target_items and not target_coverage_item_ids:
            reasons.append(RejectionReason.MISSING_TARGET_ITEMS)

        # 4. Missing rationale (for non-stop decisions)
        if (
            self.require_rationale
            and rationale is not None
            and not rationale.strip()
            and decision_type not in ("stop_partial", "stop_failed")
        ):
            reasons.append(RejectionReason.MISSING_RATIONALE)

        # 5. Revision staleness
        if current_run_revision < 0:
            reasons.append(RejectionReason.STALE_RUN_REVISION)
        if current_coverage_revision < 0:
            reasons.append(RejectionReason.STALE_COVERAGE_REVISION)

        # 6. Scope expansion check
        result = self._check_scope_expansion(scope_expansion, reasons)

        # 7. Duplicate action check
        if existing_query_signatures is not None:
            dup_reasons = self._check_duplicate_queries(
                proposed_queries, existing_query_signatures
            )
            reasons.extend(dup_reasons)

        if existing_candidate_ids is not None:
            dup_reasons = self._check_duplicate_candidates(
                proposed_candidate_ids, existing_candidate_ids
            )
            reasons.extend(dup_reasons)

        if existing_retrieval_queries is not None:
            dup_reasons = self._check_duplicate_retrieval(
                proposed_retrieval_queries, existing_retrieval_queries
            )
            reasons.extend(dup_reasons)

        # 8. Budget check
        budget_decision = None
        if estimated_cost and budget_snapshot is not None:
            budget_decision = self.budget_policy.authorize(
                budget_snapshot, estimated_cost
            )
            if not budget_decision.accepted:
                reasons.append(RejectionReason.BUDGET_EXCEEDED)

        if reasons:
            return ValidationResult.rejected(*reasons, budget_decision=budget_decision)
        return ValidationResult.accepted(
            scope_expansion=result, budget_decision=budget_decision
        )

    # ------------------------------------------------------------------
    # Revision validation
    # ------------------------------------------------------------------

    def validate_revision(
        self,
        run_revision: int,
        coverage_revision: int,
        *,
        current_run_revision: int,
        current_coverage_revision: int,
    ) -> list[RejectionReason]:
        """Check run and coverage revision staleness.

        Returns a list of rejection reasons (empty if valid).
        """
        reasons: list[RejectionReason] = []
        if current_run_revision < 0:
            reasons.append(RejectionReason.STALE_RUN_REVISION)
        if current_coverage_revision < 0:
            reasons.append(RejectionReason.STALE_COVERAGE_REVISION)
        return reasons

    # ------------------------------------------------------------------
    # Scope expansion validation
    # ------------------------------------------------------------------

    def validate_scope(
        self,
        scope_expansion: ScopeExpansionRationale | None,
        *,
        proposal_requires_expansion: bool,
    ) -> ScopeExpansionRationale | None:
        """Validate scope expansion rationale.

        Returns the scope expansion rationale if valid, or None if no
        expansion is needed.  Raises ``ScopeExpansionError`` if expansion
        is required but missing or unjustified.
        """
        if not proposal_requires_expansion:
            return None
        if scope_expansion is None:
            raise ScopeExpansionError(
                "scope expansion required but no rationale provided"
            )
        if not scope_expansion.approved:
            raise ScopeExpansionError(
                f"scope expansion {scope_expansion.expansion_type.value} "
                "not approved by policy"
            )
        return scope_expansion

    # ------------------------------------------------------------------
    # Budget validation
    # ------------------------------------------------------------------

    def validate_budget(
        self,
        estimated_cost: dict[str, int],
        budget_snapshot: BudgetSnapshot,
    ) -> BudgetDecision:
        """Check estimated cost against effective hard limits."""
        return self.budget_policy.authorize(budget_snapshot, estimated_cost)

    # ------------------------------------------------------------------
    # Query novelty validation
    # ------------------------------------------------------------------

    def validate_queries(
        self,
        proposed_queries: list[dict[str, Any]],
        existing_signatures: list[str],
    ) -> list[RejectionReason]:
        """Check proposed queries against existing signatures."""
        reasons: list[RejectionReason] = []
        for query in proposed_queries:
            query_text = query.get("query", "")
            if query_text in existing_signatures:
                reasons.append(RejectionReason.DUPLICATE_ACTION)
        return reasons

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_valid_decision_type(self, decision_type: str) -> bool:
        valid_types = {
            "search",
            "scrape",
            "retrieve",
            "synthesize",
            "stop_partial",
            "stop_failed",
        }
        return decision_type in valid_types

    def _check_scope_expansion(
        self,
        scope_expansion: ScopeExpansionRationale | None,
        reasons: list[RejectionReason],
    ) -> ScopeExpansionRationale | None:
        """Check scope expansion rationale and return it if valid."""
        if scope_expansion is not None and not scope_expansion.approved:
            reasons.append(RejectionReason.SCOPE_EXPANSION_UNJUSTIFIED)
        return scope_expansion

    def _check_duplicate_queries(
        self,
        proposed_queries: list[dict[str, Any]],
        existing_signatures: list[str],
    ) -> list[RejectionReason]:
        reasons: list[RejectionReason] = []
        for query in proposed_queries:
            query_text = query.get("query", "")
            if query_text in existing_signatures:
                reasons.append(RejectionReason.DUPLICATE_ACTION)
        return reasons

    def _check_duplicate_candidates(
        self,
        proposed_candidate_ids: list[UUID],
        existing_candidate_ids: set[UUID],
    ) -> list[RejectionReason]:
        reasons: list[RejectionReason] = []
        for cid in proposed_candidate_ids:
            if cid in existing_candidate_ids:
                reasons.append(RejectionReason.DUPLICATE_ACTION)
        return reasons

    def _check_duplicate_retrieval(
        self,
        proposed_retrieval_queries: list[str],
        existing_retrieval_queries: set[str],
    ) -> list[RejectionReason]:
        reasons: list[RejectionReason] = []
        for rq in proposed_retrieval_queries:
            if rq in existing_retrieval_queries:
                reasons.append(RejectionReason.DUPLICATE_ACTION)
        return reasons


__all__ = [
    "StrategyRevisionValidator",
    "ValidationResult",
    "StrategyValidationError",
    "StaleRunRevisionError",
    "StaleCoverageRevisionError",
    "UnknownCoverageItemError",
    "UnknownRunError",
    "DuplicateActionError",
    "BudgetExceededError",
    "ScopeExpansionError",
    "TerminalRunStateError",
]
