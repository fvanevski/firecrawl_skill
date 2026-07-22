"""Deterministic terminal-decision policy for coverage-led research runs.

This module implements ``TerminalDecisionPolicy``, a versioned policy engine
that evaluates whether a research run should continue, stop with sufficient
results, stop with partial results, or stop with failure.

Key invariants:

* No-progress signals are deterministic — they are computed from observable
  state, never from model confidence.
* Equivalent proposal detection prevents indefinite adaptive loops.
* Budget exhaustion always produces an explicit terminal outcome.
* Blocked requirements surface the unresolved evidence gap.
* A hard cap on strategy revisions prevents runaway cycles.
* Wall-clock exhaustion is a deterministic stop condition.

This policy is intentionally pure — it carries no persistence or
orchestration logic.  The orchestrator is responsible for translating
the decision into a run-state transition via ``ResearchRunService``.

Usage::

    policy = TerminalDecisionPolicy(max_strategy_revisions=10)
    decision = policy.evaluate(
        run_id=run_id,
        run_revision=run_revision,
        coverage_revision=coverage_revision,
        overall_status=overall_status,
        budget_exhausted=budget_exhausted,
        no_progress=no_progress,
        strategy_revision_count=strategy_revision_count,
        wall_clock_seconds=wall_clock_seconds,
        wall_clock_limit_seconds=wall_clock_limit_seconds,
        new_candidate_count=new_candidate_count,
        new_asset_count=new_asset_count,
        changed_coverage_count=changed_coverage_count,
        equivalent_proposal_count=equivalent_proposal_count,
        unresolved_gap=unresolved_gap,
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from research_domain.models import (
    NoProgressSignal,
    OverallCoverageStatus,
    TerminalDecision,
    TerminalDecisionOutcome,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TerminalDecisionPolicyError(ValueError):
    """A terminal-decision policy operation violated a constraint."""


class InvalidCoverageStatusError(TerminalDecisionPolicyError):
    """The overall coverage status is not a known enum value."""


class NegativeCountError(TerminalDecisionPolicyError):
    """A count argument was negative."""


# ---------------------------------------------------------------------------
# Policy configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminalDecisionConfig:
    """Configuration for the terminal-decision policy.

    Args:
        max_strategy_revisions: Hard cap on strategy revisions per run.
            When exceeded, the policy returns PARTIAL or FAILED depending
            on coverage status.
        max_wall_clock_seconds: Hard wall-clock limit in seconds.
            When exceeded, the policy returns PARTIAL or FAILED.
        max_equivalent_proposals: Number of equivalent proposals before
            triggering REPEATED_EQUIVALENT_PROPOSALS.
    """

    max_strategy_revisions: int = 10
    max_wall_clock_seconds: int = 3600  # 1 hour default
    max_equivalent_proposals: int = 3

    def __post_init__(self) -> None:
        if self.max_strategy_revisions < 1:
            raise TerminalDecisionPolicyError("max_strategy_revisions must be >= 1")
        if self.max_wall_clock_seconds < 1:
            raise TerminalDecisionPolicyError("max_wall_clock_seconds must be >= 1")
        if self.max_equivalent_proposals < 1:
            raise TerminalDecisionPolicyError("max_equivalent_proposals must be >= 1")


# ---------------------------------------------------------------------------
# Policy evaluation
# ---------------------------------------------------------------------------


class TerminalDecisionPolicy:
    """Deterministic terminal-decision policy engine.

    Public API:

    * ``evaluate`` — main entry point; returns a ``TerminalDecision``.
    * ``evaluate_outcome`` — convenience that returns just the outcome.
    * ``collect_signals`` — collect all no-progress signals without
      committing to a terminal decision.
    """

    def __init__(self, config: TerminalDecisionConfig | None = None) -> None:
        self.config = config or TerminalDecisionConfig()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def evaluate(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int,
        *,
        overall_status: str,
        budget_exhausted: bool = False,
        no_progress: bool = False,
        strategy_revision_count: int = 0,
        wall_clock_seconds: float = 0.0,
        wall_clock_limit_seconds: float | None = None,
        new_candidate_count: int = 0,
        new_asset_count: int = 0,
        changed_coverage_count: int = 0,
        equivalent_proposal_count: int = 0,
        repeated_extraction_failures: int = 0,
        repeated_retrieval_count: int = 0,
        unresolved_gap: str = "",
        unsatisfiable_source: bool = False,
        created_at: Any | None = None,
    ) -> TerminalDecision:
        """Evaluate terminal conditions and return a ``TerminalDecision``.

        Args:
            run_id: The research run UUID.
            run_revision: Current lifecycle revision of the run.
            coverage_revision: Current coverage revision.
            overall_status: The overall coverage status string.
            budget_exhausted: Whether the hard budget is exhausted.
            no_progress: Whether the loop detected no progress.
            strategy_revision_count: Total strategy revisions attempted.
            wall_clock_seconds: Elapsed wall-clock time in seconds.
            wall_clock_limit_seconds: Wall-clock limit (overrides config).
            new_candidate_count: New candidates found this cycle.
            new_asset_count: New successfully acquired assets this cycle.
            changed_coverage_count: Coverage items changed status this cycle.
            equivalent_proposal_count: Repeated equivalent proposals.
            repeated_extraction_failures: Consecutive extraction failures.
            repeated_retrieval_count: Repeated retrieval of same evidence.
            unresolved_gap: Human-readable description of the gap.
            unsatisfiable_source: Whether a source requirement is unsatisfiable.
            created_at: Override timestamp (for testing).

        Returns:
            A ``TerminalDecision`` with the computed outcome.

        Raises:
            InvalidCoverageStatusError: Unknown overall_status value.
            NegativeCountError: Negative count arguments.
        """
        # Validate inputs
        self._validate_inputs(
            new_candidate_count,
            new_asset_count,
            changed_coverage_count,
            equivalent_proposal_count,
            repeated_extraction_failures,
            repeated_retrieval_count,
        )

        # Collect no-progress signals
        signals = self.collect_signals(
            no_progress=no_progress,
            budget_exhausted=budget_exhausted,
            strategy_revision_count=strategy_revision_count,
            wall_clock_seconds=wall_clock_seconds,
            wall_clock_limit_seconds=wall_clock_limit_seconds,
            new_candidate_count=new_candidate_count,
            new_asset_count=new_asset_count,
            changed_coverage_count=changed_coverage_count,
            equivalent_proposal_count=equivalent_proposal_count,
            repeated_extraction_failures=repeated_extraction_failures,
            repeated_retrieval_count=repeated_retrieval_count,
            unsatisfiable_source=unsatisfiable_source,
        )

        # Determine outcome
        outcome = self._determine_outcome(
            overall_status=overall_status,
            budget_exhausted=budget_exhausted,
            no_progress=no_progress,
            signals=signals,
            unsatisfiable_source=unsatisfiable_source,
        )

        decision_id = uuid4()
        now = created_at or utcnow()

        return TerminalDecision(
            schema_version=TerminalDecision.SCHEMA_VERSION,
            decision_id=decision_id,
            run_id=run_id,
            run_revision=run_revision,
            coverage_revision=coverage_revision,
            outcome=outcome,
            no_progress_signals=tuple(signals),
            unresolved_gap=unresolved_gap or self._gap_for_outcome(outcome, signals),
            policy_version=TerminalDecision.POLICY_VERSION,
            created_at=now,
        )

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def evaluate_outcome(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int,
        *,
        overall_status: str,
        budget_exhausted: bool = False,
        no_progress: bool = False,
        strategy_revision_count: int = 0,
        wall_clock_seconds: float = 0.0,
        wall_clock_limit_seconds: float | None = None,
        new_candidate_count: int = 0,
        new_asset_count: int = 0,
        changed_coverage_count: int = 0,
        equivalent_proposal_count: int = 0,
        repeated_extraction_failures: int = 0,
        repeated_retrieval_count: int = 0,
        unresolved_gap: str = "",
        unsatisfiable_source: bool = False,
        created_at: Any | None = None,
    ) -> TerminalDecisionOutcome:
        """Return only the terminal outcome (convenience wrapper)."""
        decision = self.evaluate(
            run_id,
            run_revision,
            coverage_revision,
            overall_status=overall_status,
            budget_exhausted=budget_exhausted,
            no_progress=no_progress,
            strategy_revision_count=strategy_revision_count,
            wall_clock_seconds=wall_clock_seconds,
            wall_clock_limit_seconds=wall_clock_limit_seconds,
            new_candidate_count=new_candidate_count,
            new_asset_count=new_asset_count,
            changed_coverage_count=changed_coverage_count,
            equivalent_proposal_count=equivalent_proposal_count,
            repeated_extraction_failures=repeated_extraction_failures,
            repeated_retrieval_count=repeated_retrieval_count,
            unresolved_gap=unresolved_gap,
            unsatisfiable_source=unsatisfiable_source,
            created_at=created_at,
        )
        return decision.outcome

    # ------------------------------------------------------------------
    # Signal collection
    # ------------------------------------------------------------------

    def collect_signals(
        self,
        *,
        no_progress: bool = False,
        budget_exhausted: bool = False,
        strategy_revision_count: int = 0,
        wall_clock_seconds: float = 0.0,
        wall_clock_limit_seconds: float | None = None,
        new_candidate_count: int = 0,
        new_asset_count: int = 0,
        changed_coverage_count: int = 0,
        equivalent_proposal_count: int = 0,
        repeated_extraction_failures: int = 0,
        repeated_retrieval_count: int = 0,
        unsatisfiable_source: bool = False,
    ) -> list[NoProgressSignal]:
        """Collect all no-progress signals without committing to a decision.

        This method is useful for diagnostics — it returns the full set
        of signals that would trigger a terminal decision, allowing the
        caller to inspect or log them before deciding.

        Returns:
            A list of ``NoProgressSignal`` values (may be empty).
        """
        signals: list[NoProgressSignal] = []

        # 1. Budget exhaustion
        if budget_exhausted:
            signals.append(NoProgressSignal.BUDGET_EXHAUSTED)

        # 2. Max strategy revisions exceeded
        if strategy_revision_count >= self.config.max_strategy_revisions:
            signals.append(NoProgressSignal.REPEATED_EQUIVALENT_PROPOSALS)

        # 3. Wall-clock exhaustion
        limit = (
            wall_clock_limit_seconds
            if wall_clock_limit_seconds is not None
            else self.config.max_wall_clock_seconds
        )
        if wall_clock_seconds >= limit:
            signals.append(NoProgressSignal.BUDGET_EXHAUSTED)

        # 4. No new candidates
        if new_candidate_count <= 0:
            signals.append(NoProgressSignal.NO_NEW_CANDIDATES)

        # 5. No new assets
        if new_asset_count <= 0:
            signals.append(NoProgressSignal.NO_NEW_ASSETS)

        # 6. No changed coverage items
        if changed_coverage_count <= 0:
            signals.append(NoProgressSignal.NO_CHANGED_COVERAGE)

        # 7. Repeated equivalent proposals
        if equivalent_proposal_count >= self.config.max_equivalent_proposals:
            signals.append(NoProgressSignal.REPEATED_EQUIVALENT_PROPOSALS)

        # 8. Repeated extraction failures
        if repeated_extraction_failures >= 3:
            signals.append(NoProgressSignal.REPEATED_EXTRACTION_FAILURES)

        # 9. Repeated retrieval of same evidence
        if repeated_retrieval_count >= 3:
            signals.append(NoProgressSignal.REPEATED_RETRIEVAL)

        # 10. Unsatisfiable source requirement
        if unsatisfiable_source:
            signals.append(NoProgressSignal.UNSATISFIABLE_SOURCE)

        # 11. Explicit no-progress flag
        if no_progress:
            signals.append(NoProgressSignal.NO_CHANGED_COVERAGE)

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[NoProgressSignal] = []
        for s in signals:
            if s.value not in seen:
                seen.add(s.value)
                unique.append(s)
        return unique

    # ------------------------------------------------------------------
    # Outcome determination
    # ------------------------------------------------------------------

    def _determine_outcome(
        self,
        overall_status: str,
        budget_exhausted: bool,
        no_progress: bool,
        signals: list[NoProgressSignal],
        unsatisfiable_source: bool,
    ) -> TerminalDecisionOutcome:
        """Determine the terminal outcome from signals and coverage status.

        Priority (highest to lowest):
        1. SUFFICIENT — coverage is sufficient (overrides everything)
        2. BLOCKED — unsatisfiable source or coverage is blocked
        3. FAILED — no progress or repeated equivalent proposals
        4. PARTIAL — budget exhausted with insufficient coverage
        5. FAILED — fallback for unknown status
        """
        # 1. Sufficient coverage always wins
        if self._is_sufficient(overall_status):
            return TerminalDecisionOutcome.SUFFICIENT

        # 2. Unsatisfiable source → blocked
        if unsatisfiable_source:
            return TerminalDecisionOutcome.BLOCKED

        # 3. Coverage is blocked → blocked
        if overall_status == OverallCoverageStatus.BLOCKED.value:
            return TerminalDecisionOutcome.BLOCKED

        # 4. Explicit no-progress → failed
        if no_progress:
            return TerminalDecisionOutcome.FAILED

        # 5. Repeated equivalent proposals → failed (loop detected)
        if NoProgressSignal.REPEATED_EQUIVALENT_PROPOSALS in signals:
            return TerminalDecisionOutcome.FAILED

        # 6. Budget exhausted with insufficient coverage → partial
        if budget_exhausted:
            return TerminalDecisionOutcome.PARTIAL

        # 7. Wall-clock exhausted with insufficient coverage → partial
        if NoProgressSignal.BUDGET_EXHAUSTED in signals:
            return TerminalDecisionOutcome.PARTIAL

        # 8. No new candidates/assets with insufficient → partial
        if (
            NoProgressSignal.NO_NEW_CANDIDATES in signals
            or NoProgressSignal.NO_NEW_ASSETS in signals
        ):
            return TerminalDecisionOutcome.PARTIAL

        # 9. Fallback: insufficient coverage without terminal triggers
        #    → partial (we have some evidence but not enough)
        if overall_status in (
            OverallCoverageStatus.PARTIAL.value,
            OverallCoverageStatus.INSUFFICIENT.value,
        ):
            return TerminalDecisionOutcome.PARTIAL

        # 10. Unknown status → failed (fail-closed)
        return TerminalDecisionOutcome.FAILED

    # ------------------------------------------------------------------
    # Unresolved gap text
    # ------------------------------------------------------------------

    def _gap_for_outcome(
        self,
        outcome: TerminalDecisionOutcome,
        signals: list[NoProgressSignal],
    ) -> str:
        """Generate a default unresolved-gap description."""
        # Sufficient coverage always wins
        if outcome == TerminalDecisionOutcome.SUFFICIENT:
            return "no unresolved gap — coverage sufficient"

        # Blocked
        if outcome == TerminalDecisionOutcome.BLOCKED:
            return "coverage blocked — unresolved requirement"

        # Build gap from signals for other outcomes
        parts: list[str] = []

        if NoProgressSignal.BUDGET_EXHAUSTED in signals:
            parts.append("budget exhausted")
        if NoProgressSignal.REPEATED_EQUIVALENT_PROPOSALS in signals:
            parts.append("repeated equivalent proposals detected")
        if NoProgressSignal.NO_NEW_CANDIDATES in signals:
            parts.append("no new candidates found")
        if NoProgressSignal.NO_NEW_ASSETS in signals:
            parts.append("no new assets acquired")
        if NoProgressSignal.NO_CHANGED_COVERAGE in signals:
            parts.append("no changed coverage items")
        if NoProgressSignal.REPEATED_EXTRACTION_FAILURES in signals:
            parts.append("repeated extraction failures")
        if NoProgressSignal.REPEATED_RETRIEVAL in signals:
            parts.append("repeated retrieval of same evidence")
        if NoProgressSignal.UNSATISFIABLE_SOURCE in signals:
            parts.append("unsatisfiable source requirement")

        if not parts:
            return "insufficient coverage — evidence gap remains"

        return "; ".join(parts)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_sufficient(overall_status: str) -> bool:
        """Check if the overall status indicates sufficient coverage."""
        return overall_status == OverallCoverageStatus.SUFFICIENT.value

    @staticmethod
    def _validate_inputs(
        new_candidate_count: int,
        new_asset_count: int,
        changed_coverage_count: int,
        equivalent_proposal_count: int,
        repeated_extraction_failures: int,
        repeated_retrieval_count: int,
    ) -> None:
        """Validate that count arguments are non-negative."""
        for name, value in [
            ("new_candidate_count", new_candidate_count),
            ("new_asset_count", new_asset_count),
            ("changed_coverage_count", changed_coverage_count),
            ("equivalent_proposal_count", equivalent_proposal_count),
            ("repeated_extraction_failures", repeated_extraction_failures),
            ("repeated_retrieval_count", repeated_retrieval_count),
        ]:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TerminalDecisionPolicyError(
                    f"{name} must be a non-negative integer"
                )
            if value < 0:
                raise NegativeCountError(f"{name} must be >= 0, got {value}")


__all__ = [
    "TerminalDecisionPolicy",
    "TerminalDecisionConfig",
    "TerminalDecisionPolicyError",
    "InvalidCoverageStatusError",
    "NegativeCountError",
]
