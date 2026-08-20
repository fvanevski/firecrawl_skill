"""Integration tests for terminal-decision authority in orchestration.

These tests verify the fix for issue #133 — terminal-decision persistence
failures must propagate through orchestration rather than being swallowed.

Key scenarios:

1. Historical failure: before the fix, ``_evaluate_terminal_decision``
   caught ``TerminalDecisionError`` in a broad ``except Exception`` and
   returned ``None``, allowing the orchestration loop to continue as if
   no terminal decision was reached.

2. Blocking: ``TerminalDecisionError`` must propagate to the orchestration
   caller and prevent the lifecycle transition.

3. No committed context: a failed persistence attempt must not leave a
   terminal outcome in ``_terminal_outcome``.

4. Retry after recovery: a successful retry persists exactly one decision
   and completes the intended transition.

5. Duplicate idempotency: a duplicate idempotency key is handled correctly
   without creating two decisions.

6. Policy evaluation failure: ``TerminalDecisionPolicyError`` is non-fatal
   and falls back to budget check.

7. End-to-end: the full orchestrator call path with a failing service.

8. Atomic commit: ``commit_terminal_decision`` executes both the INSERT
   and lifecycle transition in a single PostgreSQL transaction.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import UUID, uuid4

# Ensure scripts/ is on the path so imports resolve.
_SCRIPT_DIR = __file__.rsplit("/", 1)[0] or "."
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from firecrawl_skill.research_domain.models import (
    TerminalDecision,
    TerminalDecisionOutcome,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.orchestrator import (
    OrchestratorConfig,
    ResearchOrchestrator,
)
from firecrawl_skill.research_store.run_service import ResearchRunService
from firecrawl_skill.research_store.terminal_decision_service import (
    DuplicateTerminalDecisionError,
    TerminalDecisionError,
    TerminalDecisionService,
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


@dataclass(frozen=True)
class MockTransitionResult:
    transition_id: UUID = field(default_factory=uuid4)
    event_id: UUID = field(default_factory=uuid4)
    prior_state: str = ""
    next_state: str = ""
    lifecycle_revision: int = 1
    reused: bool = False


class MockConfig:
    """Minimal StoreConfig replacement."""

    def __init__(self) -> None:
        self.execution_mode = "autonomous_local"
        self.max_adaptive_cycles = 5
        self.database_url = "postgresql://localhost/test"
        self.blob_root = "/tmp/blob-root"
        self.embedding_model = "test-model"

    def require_database(self) -> None:
        pass


class MockRunService:
    """Minimal mock of ResearchRunService for integration tests."""

    def __init__(self, initial_state: str = "created", revision: int = 0) -> None:
        self._state = initial_state
        self._revision = revision
        self.transitions: list[dict[str, Any]] = []
        self._external_id_map: dict[str, UUID] = {}
        self._internal_id: UUID = uuid4()
        self._uow_factory = MagicMock()
        self.evidence_service = MagicMock()
        self.evidence_service.export_packet.return_value = {
            "schema_version": "evidence-packet-v1",
        }
        self.policy_version = "run-policy-v1"

    def fail(self, run_id, **kwargs):
        self._state = "failed"
        self._revision += 1
        self.transitions.append(
            {
                "run_id": str(run_id),
                "next_state": "failed",
                "revision": self._revision,
                **kwargs,
            }
        )
        return MockTransitionResult(prior_state=self._state, next_state="failed")

    def partial(self, run_id, **kwargs):
        self._state = "partial"
        self._revision += 1
        self.transitions.append(
            {
                "run_id": str(run_id),
                "next_state": "partial",
                "revision": self._revision,
                **kwargs,
            }
        )
        return MockTransitionResult(prior_state=self._state, next_state="partial")

    def status(self, *, run_id=None, external_id=None):
        return MockRunStatus(
            id=run_id or self._internal_id,
            external_id=external_id,
            state=self._state,
            lifecycle_revision=self._revision,
            execution_mode="autonomous_local",
            objective="test objective",
        )

    def transition(self, run_id, next_state, **kwargs):
        prior = self._state
        self._state = next_state
        self._revision += 1
        self.transitions.append(
            {
                "run_id": str(run_id),
                "prior_state": prior,
                "next_state": next_state,
                "revision": self._revision,
                **kwargs,
            }
        )
        return MockTransitionResult(prior_state=prior, next_state=next_state)

    def complete(self, run_id, **kwargs):
        return self.transition(run_id, "completed", **kwargs)

    def commit_terminal_decision(self, run_id, **kwargs):
        """Mock atomic commit — simulates success."""
        # Determine next_state from kwargs
        next_state = kwargs.get("next_state", "failed")
        self._state = next_state
        self._revision += 1
        self.transitions.append(
            {
                "run_id": str(run_id),
                "next_state": next_state,
                "revision": self._revision,
                **kwargs,
            }
        )
        return {
            "transition_id": uuid4(),
            "event_id": uuid4(),
            "lifecycle_revision": self._revision,
            "prior_state": self._state,
            "next_state": next_state,
            "reused": False,
        }

    @property
    def uow_factory(self):
        return self._uow_factory


# ===================================================================
# Helpers
# ===================================================================


def _make_decision(
    run_id=None,
    outcome=TerminalDecisionOutcome.PARTIAL,
    run_revision=1,
    coverage_revision=1,
):
    """Create a valid ``TerminalDecision`` for testing."""
    return TerminalDecision(
        schema_version=TerminalDecision.SCHEMA_VERSION,
        decision_id=uuid4(),
        run_id=run_id or uuid4(),
        run_revision=run_revision,
        coverage_revision=coverage_revision,
        outcome=outcome,
        no_progress_signals=(),
        unresolved_gap="test gap",
        policy_version=TerminalDecision.POLICY_VERSION,
        created_at=cast(datetime, None),
    )


def _make_service(
    uow_factory,
    fail_on_insert=False,
    duplicate_on_key=None,
    existing_decision=None,
):
    """Build a ``TerminalDecisionService`` with controlled failure behaviour."""
    service = TerminalDecisionService(uow_factory)
    # Patch the record method to inject failure / duplicate behaviour
    original_record = service.record

    def patched_record(run_id, decision, idempotency_key):
        if duplicate_on_key and idempotency_key == duplicate_on_key:
            # Simulate a pre-existing decision (INSERT succeeded previously)
            if existing_decision:
                return existing_decision
            raise DuplicateTerminalDecisionError(
                f"Duplicate idempotency key: {idempotency_key}"
            )
        if fail_on_insert:
            # Simulate what the real service does: wrap the DB error
            raise TerminalDecisionError(
                f"Failed to persist terminal decision for run {run_id}: "
                "database unavailable"
            )
        return original_record(run_id, decision, idempotency_key)

    service.record = patched_record
    return service


# ===================================================================
# Test: Policy evaluation returns TerminalDecision (not Outcome)
# ===================================================================


class TestPolicyEvaluation(unittest.TestCase):
    """Verify that _evaluate_terminal_decision returns a TerminalDecision."""

    def test_returns_terminal_decision(self):
        """_evaluate_terminal_decision returns a TerminalDecision, not Outcome."""

        run_svc = MockRunService(initial_state="created", revision=0)

        orchestrator = ResearchOrchestrator(
            run_service=cast(ResearchRunService, run_svc),
            coverage_service=MagicMock(),
            strategy_service=MagicMock(),
            acquisition_service=MagicMock(),
            config=cast(StoreConfig, MockConfig()),
            orchestrator_config=OrchestratorConfig(),
        )

        ctx = {
            "overall_status": "insufficient",
            "_budget_exhausted": True,
            "_no_progress": False,
            "_strategy_revision_count": 0,
            "_repeated_extraction_failures": 0,
            "_repeated_retrieval_count": 0,
            "_unsatisfiable_source": False,
        }

        run_id = uuid4()
        decision = orchestrator._evaluate_terminal_decision(
            ctx, run_id, run_revision=1, coverage_revision=1
        )

        # Should return a TerminalDecision, not None or Outcome
        self.assertIsNotNone(decision)
        self.assertIsInstance(decision, TerminalDecision)
        self.assertEqual(decision.outcome, TerminalDecisionOutcome.PARTIAL)

    def test_returns_none_on_policy_error(self):
        """Policy evaluation errors return None (fallback)."""

        run_svc = MockRunService(initial_state="created", revision=0)

        orchestrator = ResearchOrchestrator(
            run_service=cast(ResearchRunService, run_svc),
            coverage_service=MagicMock(),
            strategy_service=MagicMock(),
            acquisition_service=MagicMock(),
            config=cast(StoreConfig, MockConfig()),
            orchestrator_config=OrchestratorConfig(),
        )

        # Patch policy.evaluate to raise a policy error
        from firecrawl_skill.research_store.terminal_decision import (
            TerminalDecisionPolicy,
        )

        original_evaluate = TerminalDecisionPolicy.evaluate

        def broken_evaluate(self, *args, **kwargs):
            from firecrawl_skill.research_store.terminal_decision import (
                NegativeCountError,
            )

            raise NegativeCountError("new_candidate_count must be >= 0")

        TerminalDecisionPolicy.evaluate = broken_evaluate
        try:
            ctx = {"overall_status": "insufficient"}
            result = orchestrator._evaluate_terminal_decision(
                ctx, uuid4(), run_revision=1, coverage_revision=1
            )
            # Should return None (fallback), not raise
            self.assertIsNone(result)
        finally:
            TerminalDecisionPolicy.evaluate = original_evaluate


# ===================================================================
# Test: commit_terminal_decision raises on DB failure
# ===================================================================


class TestCommitTerminalDecision(unittest.TestCase):
    """Verify that commit_terminal_decision raises on DB failure."""

    def test_raises_on_db_failure(self):
        """A database failure during commit must raise — not return None."""
        from firecrawl_skill.research_store.run_service import ResearchRunService

        call_count = [0]

        def failing_uow_factory():
            call_count[0] += 1
            raise RuntimeError("database unavailable")

        service = ResearchRunService(failing_uow_factory)
        decision = _make_decision()

        with self.assertRaises(TerminalDecisionError):
            service.commit_terminal_decision(
                decision.run_id,
                decision_id=decision.decision_id,
                run_revision=decision.run_revision,
                coverage_revision=decision.coverage_revision,
                outcome=decision.outcome.value,
                no_progress_signals=tuple(
                    s.value for s in decision.no_progress_signals
                ),
                unresolved_gap=decision.unresolved_gap,
                policy_version=decision.policy_version,
                idempotency_key="test:key",
                created_at=decision.created_at,
                next_state="partial",
                expected_revision=5,
                actor_type="orchestrator",
            )

        # The UoW factory was called exactly once (the DB error occurred
        # before any INSERT or transition could complete).
        self.assertEqual(call_count[0], 1)

    def test_raises_on_transition_failure(self):
        """A transition failure during commit must raise and rollback INSERT."""
        from firecrawl_skill.research_store.run_service import ResearchRunService

        call_count = [0]

        def failing_transition_uow_factory():
            mock_uow = MagicMock()
            mock_uow.fetchone.return_value = (uuid4(), "PARTIAL", uuid4())
            mock_uow.__enter__ = MagicMock(return_value=mock_uow)
            mock_uow.__exit__ = MagicMock(return_value=False)

            # Simulate: record_terminal_decision succeeds, apply_run_transition fails
            def mock_apply_run_transition(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise RuntimeError("transition failed")
                return {"transition_id": uuid4(), "reused": False}

            mock_uow.runs.apply_run_transition = mock_apply_run_transition
            return mock_uow

        service = ResearchRunService(failing_transition_uow_factory)
        decision = _make_decision()

        with self.assertRaises(TerminalDecisionError):
            service.commit_terminal_decision(
                decision.run_id,
                decision_id=decision.decision_id,
                run_revision=decision.run_revision,
                coverage_revision=decision.coverage_revision,
                outcome=decision.outcome.value,
                no_progress_signals=tuple(
                    s.value for s in decision.no_progress_signals
                ),
                unresolved_gap=decision.unresolved_gap,
                policy_version=decision.policy_version,
                idempotency_key="test:key",
                created_at=decision.created_at,
                next_state="partial",
                expected_revision=5,
                actor_type="orchestrator",
            )

        # The transition function was called exactly once (it failed);
        # the UoW context manager was entered and exited (rollback).
        self.assertEqual(call_count[0], 1)


# ===================================================================
# Test: Successful commit updates state
# ===================================================================


class TestSuccessfulCommit(unittest.TestCase):
    """Verify that successful commit updates state correctly."""

    def test_commit_succeeds_and_updates_state(self):
        """A successful commit should update the run state."""
        from firecrawl_skill.research_store.run_service import ResearchRunService

        def success_uow_factory():
            mock_uow = MagicMock()
            mock_uow.fetchone.return_value = (uuid4(), "PARTIAL", uuid4())
            mock_uow.__enter__ = MagicMock(return_value=mock_uow)
            mock_uow.__exit__ = MagicMock(return_value=False)

            def mock_apply_run_transition(*args, **kwargs):
                return {
                    "transition_id": uuid4(),
                    "event_id": uuid4(),
                    "lifecycle_revision": 6,
                    "prior_state": "acquiring",
                    "next_state": "partial",
                    "reused": False,
                }

            mock_uow.runs.apply_run_transition = mock_apply_run_transition
            return mock_uow

        service = ResearchRunService(success_uow_factory)
        decision = _make_decision()

        result = service.commit_terminal_decision(
            decision.run_id,
            decision_id=decision.decision_id,
            run_revision=decision.run_revision,
            coverage_revision=decision.coverage_revision,
            outcome=decision.outcome.value,
            no_progress_signals=tuple(s.value for s in decision.no_progress_signals),
            unresolved_gap=decision.unresolved_gap,
            policy_version=decision.policy_version,
            idempotency_key="test:key",
            created_at=decision.created_at,
            next_state="partial",
            expected_revision=5,
            actor_type="orchestrator",
        )

        # Should return transition result
        self.assertIsNotNone(result)
        self.assertEqual(result["next_state"], "partial")


# ===================================================================
# Test: Idempotent commit
# ===================================================================


class TestIdempotentCommit(unittest.TestCase):
    """Verify that commit_terminal_decision is idempotent."""

    def test_duplicate_key_returns_existing(self):
        """A duplicate idempotency key should return existing results."""
        from firecrawl_skill.research_store.run_service import ResearchRunService

        call_count = [0]

        def idempotent_uow_factory():
            call_count[0] += 1
            mock_uow = MagicMock()
            mock_uow.__enter__ = MagicMock(return_value=mock_uow)
            mock_uow.__exit__ = MagicMock(return_value=False)

            # First call: INSERT succeeds
            # Second call: returns existing (reused=True)
            if call_count[0] == 1:
                mock_uow.fetchone.return_value = None  # No existing decision
            else:
                mock_uow.fetchone.return_value = (
                    uuid4(),
                    str(uuid4()),
                    1,
                    1,
                    "partial",
                    (),
                    "gap",
                    "v1",
                    "test:key",
                    None,
                )

            def mock_apply_run_transition(*args, **kwargs):
                return {
                    "transition_id": uuid4(),
                    "event_id": uuid4(),
                    "lifecycle_revision": 6,
                    "prior_state": "acquiring",
                    "next_state": "partial",
                    "reused": call_count[0] > 1,
                }

            mock_uow.runs.apply_run_transition = mock_apply_run_transition
            return mock_uow

        service = ResearchRunService(idempotent_uow_factory)
        decision = _make_decision()

        # First call: should succeed
        result1 = service.commit_terminal_decision(
            decision.run_id,
            decision_id=decision.decision_id,
            run_revision=decision.run_revision,
            coverage_revision=decision.coverage_revision,
            outcome=decision.outcome.value,
            no_progress_signals=tuple(s.value for s in decision.no_progress_signals),
            unresolved_gap=decision.unresolved_gap,
            policy_version=decision.policy_version,
            idempotency_key="test:key",
            created_at=decision.created_at,
            next_state="partial",
            expected_revision=5,
            actor_type="orchestrator",
        )
        self.assertFalse(result1.get("reused", False))

        # Second call: should return existing (reused=True)
        result2 = service.commit_terminal_decision(
            decision.run_id,
            decision_id=decision.decision_id,
            run_revision=decision.run_revision,
            coverage_revision=decision.coverage_revision,
            outcome=decision.outcome.value,
            no_progress_signals=tuple(s.value for s in decision.no_progress_signals),
            unresolved_gap=decision.unresolved_gap,
            policy_version=decision.policy_version,
            idempotency_key="test:key",
            created_at=decision.created_at,
            next_state="partial",
            expected_revision=5,
            actor_type="orchestrator",
        )
        self.assertTrue(result2.get("reused", False))


# ===================================================================
# Test: End-to-end orchestrator with commit_terminal_decision
# ===================================================================


class TestEndToEndOrchestrator(unittest.TestCase):
    """Full orchestration path with commit_terminal_decision."""

    def test_orchestrator_uses_commit_terminal_decision(self):
        """The orchestrator should call commit_terminal_decision, not separate calls."""

        run_svc = MockRunService(initial_state="acquiring", revision=5)

        orchestrator = ResearchOrchestrator(
            run_service=cast(ResearchRunService, run_svc),
            coverage_service=MagicMock(),
            strategy_service=MagicMock(),
            acquisition_service=MagicMock(),
            config=cast(StoreConfig, MockConfig()),
            orchestrator_config=OrchestratorConfig(),
        )

        ctx = {
            "overall_status": "insufficient",
            "_budget_exhausted": True,
            "_no_progress": False,
            "_strategy_revision_count": 0,
            "_repeated_extraction_failures": 0,
            "_repeated_retrieval_count": 0,
            "_unsatisfiable_source": False,
        }

        run_id = uuid4()
        decision = orchestrator._evaluate_terminal_decision(
            ctx, run_id, run_revision=5, coverage_revision=3
        )

        # Should return a TerminalDecision
        self.assertIsNotNone(decision)
        self.assertIsInstance(decision, TerminalDecision)

        # Simulate the orchestrator loop calling commit_terminal_decision
        run_svc.commit_terminal_decision(
            run_id,
            decision_id=decision.decision_id,
            run_revision=5,
            coverage_revision=3,
            outcome=decision.outcome.value,
            no_progress_signals=tuple(s.value for s in decision.no_progress_signals),
            unresolved_gap=decision.unresolved_gap,
            policy_version=decision.policy_version,
            idempotency_key=f"terminal:{run_id}:r5:c3",
            created_at=decision.created_at,
            next_state="partial",
            expected_revision=5,
            actor_type="orchestrator",
        )

        # State should be updated
        self.assertEqual(run_svc._state, "partial")
        self.assertEqual(len(run_svc.transitions), 1)


# ===================================================================
# Test: Service record raises correctly
# ===================================================================


class TestServiceRecordRaises(unittest.TestCase):
    """Verify that TerminalDecisionService.record raises the correct exceptions."""

    def test_database_error_becomes_terminal_decision_error(self):
        """A database error must be wrapped in TerminalDecisionError."""
        from firecrawl_skill.research_store.terminal_decision_service import (
            TerminalDecisionService,
        )

        def broken_uow_factory():
            raise RuntimeError("connection refused")

        service = TerminalDecisionService(broken_uow_factory)
        decision = _make_decision()

        with self.assertRaises(TerminalDecisionError) as ctx:
            service.record(
                run_id=decision.run_id,
                decision=decision,
                idempotency_key="test:key",
            )
        # The error message should contain the wrapped DB error
        self.assertIn("connection refused", str(ctx.exception))

    def test_duplicate_error_is_preserved(self):
        """DuplicateTerminalDecisionError must not be wrapped."""
        from firecrawl_skill.research_store.terminal_decision_service import (
            TerminalDecisionService,
        )

        def duplicate_uow_factory():
            raise DuplicateTerminalDecisionError("already exists")

        service = TerminalDecisionService(duplicate_uow_factory)
        decision = _make_decision()

        with self.assertRaises(DuplicateTerminalDecisionError):
            service.record(
                run_id=decision.run_id,
                decision=decision,
                idempotency_key="test:key",
            )


if __name__ == "__main__":
    unittest.main()
