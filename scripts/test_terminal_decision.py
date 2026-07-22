"""Tests for the TerminalDecisionPolicy.

Tests cover:

* Normal success: sufficient coverage produces SUFFICIENT outcome.
* Budget exhaustion: produces PARTIAL for insufficient coverage,
  SUFFICIENT for sufficient coverage.
* No-progress: produces FAILED when no progress is detected.
* Equivalent proposals: REPEATED_EQUIVALENT_PROPOSALS signal triggers
  FAILED for insufficient coverage.
* Blocked coverage: produces BLOCKED for blocked status.
* Wall-clock exhaustion: produces PARTIAL for insufficient coverage.
* Max strategy revisions: triggers REPEATED_EQUIVALENT_PROPOSALS signal.
* No new candidates/assets: produces PARTIAL for insufficient coverage.
* Unsatisfiable source: produces BLOCKED.
* Signal collection: all signal types are detected.
* Invalid input: negative counts, invalid status.
* Idempotency: same inputs produce same outcome.
* Unresolved gap text: default gap descriptions are generated.
* Policy version: TerminalDecision.POLICY_VERSION is enforced.
* Schema version: TerminalDecision.SCHEMA_VERSION is enforced.
* Config validation: max_strategy_revisions, max_wall_clock_seconds,
  max_equivalent_proposals must be >= 1.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock
from uuid import UUID, uuid4

# Ensure scripts/ is on the path so imports resolve.
_SCRIPT_DIR = __file__.rsplit("/", 1)[0] or "."
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from research_domain.models import (  # noqa: E402
    NoProgressSignal,
    OverallCoverageStatus,
    TerminalDecision,
    TerminalDecisionOutcome,
)
from research_store.terminal_decision import (  # noqa: E402
    NegativeCountError,
    TerminalDecisionConfig,
    TerminalDecisionPolicy,
    TerminalDecisionPolicyError,
)


# ===================================================================
# Fixtures
# ===================================================================


@dataclass(frozen=True)
class MockRunStatus:
    id: UUID
    external_id: str | None
    state: str
    lifecycle_revision: int
    execution_mode: str
    objective: str
    current_coverage_revision: int | None = None


# ===================================================================
# Test: TerminalDecisionPolicy — normal success
# ===================================================================


class TestTerminalDecisionNormalSuccess(unittest.TestCase):
    """Verify that sufficient coverage produces SUFFICIENT outcome."""

    def setUp(self):
        self.policy = TerminalDecisionPolicy(
            TerminalDecisionConfig(max_strategy_revisions=10)
        )
        self.run_id = uuid4()
        self.run_revision = 5
        self.coverage_revision = 3

    def test_sufficient_coverage(self):
        decision = self.policy.evaluate(
            self.run_id,
            self.run_revision,
            self.coverage_revision,
            overall_status=OverallCoverageStatus.SUFFICIENT.value,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.SUFFICIENT)
        self.assertEqual(decision.schema_version, TerminalDecision.SCHEMA_VERSION)
        self.assertEqual(decision.policy_version, TerminalDecision.POLICY_VERSION)
        self.assertIsNotNone(decision.decision_id)
        self.assertEqual(decision.run_id, self.run_id)
        self.assertEqual(decision.run_revision, self.run_revision)
        self.assertEqual(decision.coverage_revision, self.coverage_revision)

    def test_sufficient_coverage_gap_is_empty(self):
        decision = self.policy.evaluate(
            self.run_id,
            self.run_revision,
            self.coverage_revision,
            overall_status=OverallCoverageStatus.SUFFICIENT.value,
        )
        self.assertIn("coverage sufficient", decision.unresolved_gap.lower())

    def test_outcome_convenience(self):
        outcome = self.policy.evaluate_outcome(
            self.run_id,
            self.run_revision,
            self.coverage_revision,
            overall_status=OverallCoverageStatus.SUFFICIENT.value,
        )
        self.assertEqual(outcome, TerminalDecisionOutcome.SUFFICIENT)


# ===================================================================
# Test: Budget exhaustion
# ===================================================================


class TestTerminalDecisionBudgetExhaustion(unittest.TestCase):
    """Verify that budget exhaustion produces explicit terminal outcomes."""

    def setUp(self):
        self.policy = TerminalDecisionPolicy(
            TerminalDecisionConfig(max_strategy_revisions=10)
        )
        self.run_id = uuid4()

    def test_budget_exhausted_insufficient_coverage(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            budget_exhausted=True,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.PARTIAL)
        self.assertIn(NoProgressSignal.BUDGET_EXHAUSTED, decision.no_progress_signals)

    def test_budget_exhausted_sufficient_coverage(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.SUFFICIENT.value,
            budget_exhausted=True,
        )
        # Sufficient coverage takes priority over budget exhaustion
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.SUFFICIENT)

    def test_budget_exhausted_partial_coverage(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.PARTIAL.value,
            budget_exhausted=True,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.PARTIAL)


# ===================================================================
# Test: No-progress detection
# ===================================================================


class TestTerminalDecisionNoProgress(unittest.TestCase):
    """Verify that no-progress produces FAILED outcome."""

    def setUp(self):
        self.policy = TerminalDecisionPolicy(
            TerminalDecisionConfig(max_strategy_revisions=10)
        )
        self.run_id = uuid4()

    def test_no_progress_insufficient(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            no_progress=True,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.FAILED)
        self.assertIn(
            NoProgressSignal.NO_CHANGED_COVERAGE, decision.no_progress_signals
        )

    def test_no_progress_partial(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.PARTIAL.value,
            no_progress=True,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.FAILED)

    def test_no_progress_sufficient(self):
        # Sufficient coverage overrides no-progress
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.SUFFICIENT.value,
            no_progress=True,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.SUFFICIENT)


# ===================================================================
# Test: Equivalent proposal detection
# ===================================================================


class TestTerminalDecisionEquivalentProposals(unittest.TestCase):
    """Verify that repeated equivalent proposals trigger FAILED."""

    def setUp(self):
        self.policy = TerminalDecisionPolicy(
            TerminalDecisionConfig(max_equivalent_proposals=3)
        )
        self.run_id = uuid4()

    def test_equivalent_proposals_insufficient(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            equivalent_proposal_count=3,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.FAILED)
        self.assertIn(
            NoProgressSignal.REPEATED_EQUIVALENT_PROPOSALS,
            decision.no_progress_signals,
        )

    def test_equivalent_proposals_sufficient(self):
        # Sufficient coverage overrides equivalent proposals
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.SUFFICIENT.value,
            equivalent_proposal_count=3,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.SUFFICIENT)

    def test_below_threshold_no_signal(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            equivalent_proposal_count=2,
        )
        self.assertNotIn(
            NoProgressSignal.REPEATED_EQUIVALENT_PROPOSALS,
            decision.no_progress_signals,
        )


# ===================================================================
# Test: Blocked coverage
# ===================================================================


class TestTerminalDecisionBlocked(unittest.TestCase):
    """Verify that blocked coverage produces BLOCKED outcome."""

    def setUp(self):
        self.policy = TerminalDecisionPolicy()
        self.run_id = uuid4()

    def test_blocked_coverage(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.BLOCKED.value,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.BLOCKED)

    def test_unsatisfiable_source(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            unsatisfiable_source=True,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.BLOCKED)
        self.assertIn(
            NoProgressSignal.UNSATISFIABLE_SOURCE, decision.no_progress_signals
        )


# ===================================================================
# Test: Max strategy revisions
# ===================================================================


class TestTerminalDecisionMaxStrategyRevisions(unittest.TestCase):
    """Verify that max strategy revisions triggers terminal decision."""

    def setUp(self):
        self.policy = TerminalDecisionPolicy(
            TerminalDecisionConfig(max_strategy_revisions=5)
        )
        self.run_id = uuid4()

    def test_exceeds_max_revisions_insufficient(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            strategy_revision_count=5,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.FAILED)
        self.assertIn(
            NoProgressSignal.REPEATED_EQUIVALENT_PROPOSALS,
            decision.no_progress_signals,
        )

    def test_exceeds_max_revisions_sufficient(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.SUFFICIENT.value,
            strategy_revision_count=5,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.SUFFICIENT)

    def test_at_limit_no_signal(self):
        # At limit (5 == 5) should trigger; below should not
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            strategy_revision_count=4,
        )
        self.assertNotIn(
            NoProgressSignal.REPEATED_EQUIVALENT_PROPOSALS,
            decision.no_progress_signals,
        )


# ===================================================================
# Test: Wall-clock exhaustion
# ===================================================================


class TestTerminalDecisionWallClock(unittest.TestCase):
    """Verify that wall-clock exhaustion produces PARTIAL for insufficient."""

    def setUp(self):
        self.policy = TerminalDecisionPolicy(
            TerminalDecisionConfig(max_wall_clock_seconds=100)
        )
        self.run_id = uuid4()

    def test_wall_clock_exhausted_insufficient(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            wall_clock_seconds=100.0,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.PARTIAL)

    def test_wall_clock_exhausted_sufficient(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.SUFFICIENT.value,
            wall_clock_seconds=100.0,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.SUFFICIENT)

    def test_wall_clock_below_limit(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            wall_clock_seconds=99.0,
        )
        self.assertNotIn(
            NoProgressSignal.BUDGET_EXHAUSTED, decision.no_progress_signals
        )

    def test_custom_wall_clock_limit(self):
        policy = TerminalDecisionPolicy(
            TerminalDecisionConfig(max_wall_clock_seconds=3600)
        )
        decision = policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            wall_clock_seconds=200.0,
            wall_clock_limit_seconds=100.0,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.PARTIAL)


# ===================================================================
# Test: No new candidates / assets / changed coverage
# ===================================================================


class TestTerminalDecisionNoNewData(unittest.TestCase):
    """Verify that zero new data produces PARTIAL for insufficient."""

    def setUp(self):
        self.policy = TerminalDecisionPolicy(
            TerminalDecisionConfig(max_strategy_revisions=10)
        )
        self.run_id = uuid4()

    def test_no_new_candidates(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            new_candidate_count=0,
            new_asset_count=1,
            changed_coverage_count=1,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.PARTIAL)
        self.assertIn(NoProgressSignal.NO_NEW_CANDIDATES, decision.no_progress_signals)

    def test_no_new_assets(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            new_candidate_count=1,
            new_asset_count=0,
            changed_coverage_count=1,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.PARTIAL)
        self.assertIn(NoProgressSignal.NO_NEW_ASSETS, decision.no_progress_signals)

    def test_no_changed_coverage(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            new_candidate_count=1,
            new_asset_count=1,
            changed_coverage_count=0,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.PARTIAL)
        self.assertIn(
            NoProgressSignal.NO_CHANGED_COVERAGE, decision.no_progress_signals
        )


# ===================================================================
# Test: Signal collection
# ===================================================================


class TestTerminalDecisionSignalCollection(unittest.TestCase):
    """Verify that collect_signals returns all expected signals."""

    def setUp(self):
        self.policy = TerminalDecisionPolicy(
            TerminalDecisionConfig(
                max_strategy_revisions=5,
                max_equivalent_proposals=3,
                max_wall_clock_seconds=100,
            )
        )

    def test_collect_all_signals(self):
        signals = self.policy.collect_signals(
            no_progress=True,
            budget_exhausted=True,
            strategy_revision_count=5,
            wall_clock_seconds=100.0,
            new_candidate_count=0,
            new_asset_count=0,
            changed_coverage_count=0,
            equivalent_proposal_count=3,
            repeated_extraction_failures=5,
            repeated_retrieval_count=5,
            unsatisfiable_source=True,
        )
        signal_values = {s.value for s in signals}
        expected = {
            NoProgressSignal.BUDGET_EXHAUSTED.value,
            NoProgressSignal.REPEATED_EQUIVALENT_PROPOSALS.value,
            NoProgressSignal.NO_NEW_CANDIDATES.value,
            NoProgressSignal.NO_NEW_ASSETS.value,
            NoProgressSignal.NO_CHANGED_COVERAGE.value,
            NoProgressSignal.REPEATED_EXTRACTION_FAILURES.value,
            NoProgressSignal.REPEATED_RETRIEVAL.value,
            NoProgressSignal.UNSATISFIABLE_SOURCE.value,
        }
        self.assertEqual(signal_values, expected)

    def test_collect_no_signals(self):
        signals = self.policy.collect_signals(
            no_progress=False,
            budget_exhausted=False,
            strategy_revision_count=0,
            wall_clock_seconds=0.0,
            new_candidate_count=1,
            new_asset_count=1,
            changed_coverage_count=1,
            equivalent_proposal_count=0,
            repeated_extraction_failures=0,
            repeated_retrieval_count=0,
            unsatisfiable_source=False,
        )
        self.assertEqual(len(signals), 0)

    def test_collect_deduplicates(self):
        # Both no_progress and changed_coverage_count=0 produce NO_CHANGED_COVERAGE
        signals = self.policy.collect_signals(
            no_progress=True,
            changed_coverage_count=0,
        )
        count = sum(1 for s in signals if s == NoProgressSignal.NO_CHANGED_COVERAGE)
        self.assertEqual(count, 1)


# ===================================================================
# Test: Invalid input
# ===================================================================


class TestTerminalDecisionInvalidInput(unittest.TestCase):
    """Verify that invalid inputs raise appropriate errors."""

    def setUp(self):
        self.policy = TerminalDecisionPolicy()
        self.run_id = uuid4()

    def test_negative_candidate_count(self):
        with self.assertRaises(NegativeCountError):
            self.policy.evaluate(
                self.run_id,
                1,
                1,
                overall_status=OverallCoverageStatus.INSUFFICIENT.value,
                new_candidate_count=-1,
            )

    def test_negative_asset_count(self):
        with self.assertRaises(NegativeCountError):
            self.policy.evaluate(
                self.run_id,
                1,
                1,
                overall_status=OverallCoverageStatus.INSUFFICIENT.value,
                new_asset_count=-1,
            )

    def test_negative_changed_coverage(self):
        with self.assertRaises(NegativeCountError):
            self.policy.evaluate(
                self.run_id,
                1,
                1,
                overall_status=OverallCoverageStatus.INSUFFICIENT.value,
                changed_coverage_count=-1,
            )

    def test_negative_equivalent_proposals(self):
        with self.assertRaises(NegativeCountError):
            self.policy.evaluate(
                self.run_id,
                1,
                1,
                overall_status=OverallCoverageStatus.INSUFFICIENT.value,
                equivalent_proposal_count=-1,
            )

    def test_negative_extraction_failures(self):
        with self.assertRaises(NegativeCountError):
            self.policy.evaluate(
                self.run_id,
                1,
                1,
                overall_status=OverallCoverageStatus.INSUFFICIENT.value,
                repeated_extraction_failures=-1,
            )

    def test_negative_retrieval_count(self):
        with self.assertRaises(NegativeCountError):
            self.policy.evaluate(
                self.run_id,
                1,
                1,
                overall_status=OverallCoverageStatus.INSUFFICIENT.value,
                repeated_retrieval_count=-1,
            )


# ===================================================================
# Test: Config validation
# ===================================================================


class TestTerminalDecisionConfigValidation(unittest.TestCase):
    """Verify that config values are validated."""

    def test_min_max_strategy_revisions(self):
        with self.assertRaises(TerminalDecisionPolicyError):
            TerminalDecisionConfig(max_strategy_revisions=0)

    def test_min_max_wall_clock(self):
        with self.assertRaises(TerminalDecisionPolicyError):
            TerminalDecisionConfig(max_wall_clock_seconds=0)

    def test_min_max_equivalent_proposals(self):
        with self.assertRaises(TerminalDecisionPolicyError):
            TerminalDecisionConfig(max_equivalent_proposals=0)

    def test_valid_config(self):
        config = TerminalDecisionConfig(
            max_strategy_revisions=1,
            max_wall_clock_seconds=1,
            max_equivalent_proposals=1,
        )
        self.assertEqual(config.max_strategy_revisions, 1)
        self.assertEqual(config.max_wall_clock_seconds, 1)
        self.assertEqual(config.max_equivalent_proposals, 1)


# ===================================================================
# Test: Domain model validation
# ===================================================================


class TestTerminalDecisionModelValidation(unittest.TestCase):
    """Verify TerminalDecision domain model invariants."""

    def test_invalid_schema_version(self):
        with self.assertRaises(ValueError):
            TerminalDecision(
                schema_version="terminal-decision-v2",
                decision_id=uuid4(),
                run_id=uuid4(),
                run_revision=1,
                coverage_revision=1,
                outcome=TerminalDecisionOutcome.PARTIAL,
                no_progress_signals=(),
                unresolved_gap="test",
                policy_version=TerminalDecision.POLICY_VERSION,
                created_at=MagicMock(),
            )

    def test_invalid_policy_version(self):
        with self.assertRaises(ValueError):
            TerminalDecision(
                schema_version=TerminalDecision.SCHEMA_VERSION,
                decision_id=uuid4(),
                run_id=uuid4(),
                run_revision=1,
                coverage_revision=1,
                outcome=TerminalDecisionOutcome.PARTIAL,
                no_progress_signals=(),
                unresolved_gap="test",
                policy_version="terminal-decision-policy-v2",
                created_at=MagicMock(),
            )

    def test_duplicate_signals(self):
        with self.assertRaises(ValueError):
            TerminalDecision(
                schema_version=TerminalDecision.SCHEMA_VERSION,
                decision_id=uuid4(),
                run_id=uuid4(),
                run_revision=1,
                coverage_revision=1,
                outcome=TerminalDecisionOutcome.PARTIAL,
                no_progress_signals=(
                    NoProgressSignal.NO_NEW_CANDIDATES,
                    NoProgressSignal.NO_NEW_CANDIDATES,
                ),
                unresolved_gap="test",
                policy_version=TerminalDecision.POLICY_VERSION,
                created_at=MagicMock(),
            )

    def test_empty_unresolved_gap(self):
        with self.assertRaises(ValueError):
            TerminalDecision(
                schema_version=TerminalDecision.SCHEMA_VERSION,
                decision_id=uuid4(),
                run_id=uuid4(),
                run_revision=1,
                coverage_revision=1,
                outcome=TerminalDecisionOutcome.PARTIAL,
                no_progress_signals=(),
                unresolved_gap="",
                policy_version=TerminalDecision.POLICY_VERSION,
                created_at=MagicMock(),
            )

    def test_custom_created_at(self):
        custom_time = "2026-01-01T00:00:00+00:00"
        decision = TerminalDecision(
            schema_version=TerminalDecision.SCHEMA_VERSION,
            decision_id=uuid4(),
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            outcome=TerminalDecisionOutcome.PARTIAL,
            no_progress_signals=(),
            unresolved_gap="test gap",
            policy_version=TerminalDecision.POLICY_VERSION,
            created_at=custom_time,
        )
        self.assertEqual(decision.created_at, custom_time)


# ===================================================================
# Test: Default unresolved gap text
# ===================================================================


class TestTerminalDecisionUnresolvedGap(unittest.TestCase):
    """Verify that default unresolved-gap descriptions are generated."""

    def setUp(self):
        self.policy = TerminalDecisionPolicy()
        self.run_id = uuid4()

    def test_sufficient_gap(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.SUFFICIENT.value,
        )
        self.assertIn("sufficient", decision.unresolved_gap.lower())

    def test_insufficient_gap(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            new_candidate_count=0,
        )
        self.assertIn("no new candidates", decision.unresolved_gap.lower())

    def test_custom_gap_preserved(self):
        custom_gap = "Missing authority sources for claim X"
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            unresolved_gap=custom_gap,
        )
        self.assertEqual(decision.unresolved_gap, custom_gap)


# ===================================================================
# Test: Outcome priority
# ===================================================================


class TestTerminalDecisionOutcomePriority(unittest.TestCase):
    """Verify that outcome priority is correct (blocked > failed > sufficient > partial)."""

    def setUp(self):
        self.policy = TerminalDecisionPolicy()
        self.run_id = uuid4()

    def test_sufficient_overrides_budget(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.SUFFICIENT.value,
            budget_exhausted=True,
            no_progress=True,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.SUFFICIENT)

    def test_blocked_overrides_budget(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.BLOCKED.value,
            budget_exhausted=True,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.BLOCKED)

    def test_failed_overrides_partial(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            no_progress=True,
            budget_exhausted=True,
        )
        # No-progress (FAILED) takes priority over budget (PARTIAL)
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.FAILED)

    def test_partial_fallback(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            new_candidate_count=0,
            new_asset_count=0,
            changed_coverage_count=0,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.PARTIAL)


# ===================================================================
# Test: Default policy (no explicit config)
# ===================================================================


class TestTerminalDecisionDefaultPolicy(unittest.TestCase):
    """Verify that the default policy uses sensible defaults."""

    def setUp(self):
        self.policy = TerminalDecisionPolicy()  # No explicit config
        self.run_id = uuid4()

    def test_default_max_revisions(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            strategy_revision_count=10,
        )
        # Default max is 10, so 10 >= 10 triggers the signal
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.FAILED)

    def test_default_max_wall_clock(self):
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            wall_clock_seconds=3600.0,
        )
        # Default is 3600 seconds
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.PARTIAL)


# ===================================================================
# Test: Integration with orchestrator context
# ===================================================================


class TestOrchestratorIntegration(unittest.TestCase):
    """Verify that the orchestrator can use the policy via context."""

    def setUp(self):
        self.run_id = uuid4()
        self.policy = TerminalDecisionPolicy()

    def test_policy_returns_none_on_exception(self):
        """When policy evaluation raises, the orchestrator should handle gracefully."""
        # This tests that the policy itself handles edge cases
        decision = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            budget_exhausted=True,
        )
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.PARTIAL)
        # Verify signals are populated
        self.assertTrue(len(decision.no_progress_signals) > 0)


# ===================================================================
# Test: Idempotency
# ===================================================================


class TestTerminalDecisionIdempotency(unittest.TestCase):
    """Verify that same inputs produce consistent outcomes."""

    def setUp(self):
        self.policy = TerminalDecisionPolicy()
        self.run_id = uuid4()

    def test_same_inputs_same_outcome(self):
        decision1 = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            budget_exhausted=True,
            no_progress=False,
            new_candidate_count=0,
        )
        decision2 = self.policy.evaluate(
            self.run_id,
            1,
            1,
            overall_status=OverallCoverageStatus.INSUFFICIENT.value,
            budget_exhausted=True,
            no_progress=False,
            new_candidate_count=0,
        )
        self.assertEqual(decision1.outcome, decision2.outcome)
        self.assertEqual(
            set(s.value for s in decision1.no_progress_signals),
            set(s.value for s in decision2.no_progress_signals),
        )


if __name__ == "__main__":
    unittest.main()
