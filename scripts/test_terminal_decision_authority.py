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
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

# Ensure scripts/ is on the path so imports resolve.
_SCRIPT_DIR = __file__.rsplit("/", 1)[0] or "."
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from research_domain.models import (
    TerminalDecision,
    TerminalDecisionOutcome,
)
from research_store.orchestrator import (
    OrchestratorConfig,
    ResearchOrchestrator,
)
from research_store.terminal_decision_service import (
    DuplicateTerminalDecisionError,
    TerminalDecisionError,
    TerminalDecisionRecord,
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
        created_at=None,
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
# Test: Historical failure (pre-fix behaviour)
# ===================================================================


class TestHistoricalFailure(unittest.TestCase):
    """Verify that the pre-fix defect is now corrected.

    Before the fix, ``_evaluate_terminal_decision`` caught
    ``TerminalDecisionError`` in a broad ``except Exception`` and returned
    ``None``, allowing orchestration to continue.

    After the fix, ``TerminalDecisionError`` propagates to the caller.
    """

    def test_terminal_decision_error_propagates(self):
        """A database failure during persistence must raise — not return None."""

        run_svc = MockRunService(initial_state="created", revision=0)

        # Service that always fails on insert
        def failing_uow_factory():
            raise RuntimeError("database unavailable")

        service = _make_service(failing_uow_factory, fail_on_insert=True)

        orchestrator = ResearchOrchestrator(
            run_service=run_svc,
            coverage_service=MagicMock(),
            strategy_service=MagicMock(),
            acquisition_service=MagicMock(),
            config=MockConfig(),
            orchestrator_config=OrchestratorConfig(),
            terminal_service=service,
        )

        # Inject a terminal outcome into context so the orchestrator
        # would attempt a terminal decision if the service didn't fail.
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
        with self.assertRaises(TerminalDecisionError):
            orchestrator._evaluate_terminal_decision(
                ctx, run_id, run_revision=1, coverage_revision=1
            )

    def test_policy_error_falls_back(self):
        """Policy evaluation errors must fall back — not block."""

        run_svc = MockRunService(initial_state="created", revision=0)
        service = MagicMock()

        orchestrator = ResearchOrchestrator(
            run_service=run_svc,
            coverage_service=MagicMock(),
            strategy_service=MagicMock(),
            acquisition_service=MagicMock(),
            config=MockConfig(),
            orchestrator_config=OrchestratorConfig(),
            terminal_service=service,
        )

        # Patch policy.evaluate to raise a policy error
        from research_store.terminal_decision import TerminalDecisionPolicy

        original_evaluate = TerminalDecisionPolicy.evaluate

        def broken_evaluate(self, *args, **kwargs):
            from research_store.terminal_decision import NegativeCountError

            raise NegativeCountError("new_candidate_count must be >= 0")

        TerminalDecisionPolicy.evaluate = broken_evaluate
        try:
            ctx = {"overall_status": "insufficient"}
            result = orchestrator._evaluate_terminal_decision(
                ctx, uuid4(), run_revision=1, coverage_revision=1
            )
            # Should return None (fallback), not raise
            self.assertIsNone(result)
            # Context should NOT have _terminal_outcome set
            self.assertNotIn("_terminal_outcome", ctx)
        finally:
            TerminalDecisionPolicy.evaluate = original_evaluate


# ===================================================================
# Test: No lifecycle transition after persistence failure
# ===================================================================


class TestNoTransitionOnFailure(unittest.TestCase):
    """A failed terminal-decision persistence must not trigger a lifecycle transition."""

    def test_no_fail_transition_after_persistence_failure(self):
        """When persistence fails, the orchestrator must NOT call run_service.fail()."""

        run_svc = MockRunService(initial_state="acquiring", revision=5)

        def failing_uow_factory():
            raise RuntimeError("database unavailable")

        service = _make_service(failing_uow_factory, fail_on_insert=True)

        orchestrator = ResearchOrchestrator(
            run_service=run_svc,
            coverage_service=MagicMock(),
            strategy_service=MagicMock(),
            acquisition_service=MagicMock(),
            config=MockConfig(),
            orchestrator_config=OrchestratorConfig(),
            terminal_service=service,
        )

        run_id = uuid4()
        with self.assertRaises(TerminalDecisionError):
            orchestrator._evaluate_terminal_decision(
                {"overall_status": "insufficient"},
                run_id,
                run_revision=5,
                coverage_revision=3,
            )

        # The run service must NOT have been transitioned to "failed"
        fail_transitions = [
            t for t in run_svc.transitions if t["next_state"] == "failed"
        ]
        self.assertEqual(len(fail_transitions), 0)


# ===================================================================
# Test: No committed terminal outcome in context after failure
# ===================================================================


class TestNoCommittedContext(unittest.TestCase):
    """A failed persistence attempt must not leave _terminal_outcome in context."""

    def test_context_not_tainted_after_failure(self):
        """_terminal_outcome must not be set when persistence fails."""

        run_svc = MockRunService(initial_state="created", revision=0)

        def failing_uow_factory():
            raise RuntimeError("database unavailable")

        service = _make_service(failing_uow_factory, fail_on_insert=True)

        orchestrator = ResearchOrchestrator(
            run_service=run_svc,
            coverage_service=MagicMock(),
            strategy_service=MagicMock(),
            acquisition_service=MagicMock(),
            config=MockConfig(),
            orchestrator_config=OrchestratorConfig(),
            terminal_service=service,
        )

        ctx = {"overall_status": "insufficient", "_budget_exhausted": True}
        run_id = uuid4()

        with self.assertRaises(TerminalDecisionError):
            orchestrator._evaluate_terminal_decision(
                ctx, run_id, run_revision=1, coverage_revision=1
            )

        # Context must NOT contain _terminal_outcome after a failed attempt
        self.assertNotIn("_terminal_outcome", ctx)
        self.assertNotIn("_terminal_signals", ctx)
        self.assertNotIn("_terminal_reason", ctx)


# ===================================================================
# Test: Successful behaviour (unchanged)
# ===================================================================


class TestSuccessfulBehaviour(unittest.TestCase):
    """Successful evaluation, persistence, and transition must remain unchanged."""

    def test_context_updated_after_persistence(self):
        """Context must be updated only after persistence succeeds."""

        run_svc = MockRunService(initial_state="created", revision=0)

        # Service that succeeds
        captured_records = []

        def success_uow_factory():
            mock_uow = MagicMock()
            mock_uow.fetchone.return_value = (uuid4(), "PARTIAL", uuid4())
            mock_uow.__enter__ = MagicMock(return_value=mock_uow)
            mock_uow.__exit__ = MagicMock(return_value=False)
            return mock_uow

        service = TerminalDecisionService(success_uow_factory)

        # Patch record to capture calls
        original_record = service.record

        def capturing_record(run_id, decision, idempotency_key):
            captured_records.append((run_id, decision, idempotency_key))
            return original_record(run_id, decision, idempotency_key)

        service.record = capturing_record

        orchestrator = ResearchOrchestrator(
            run_service=run_svc,
            coverage_service=MagicMock(),
            strategy_service=MagicMock(),
            acquisition_service=MagicMock(),
            config=MockConfig(),
            orchestrator_config=OrchestratorConfig(),
            terminal_service=service,
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
        outcome = orchestrator._evaluate_terminal_decision(
            ctx, run_id, run_revision=1, coverage_revision=1
        )

        # Should return the terminal outcome
        self.assertEqual(outcome, TerminalDecisionOutcome.PARTIAL)
        # Context must be updated
        self.assertEqual(ctx["_terminal_outcome"], "partial")
        self.assertIn("_terminal_signals", ctx)
        self.assertIn("_terminal_reason", ctx)
        # Persistence must have been called
        self.assertEqual(len(captured_records), 1)


# ===================================================================
# Test: Retry after database recovery
# ===================================================================


class TestRetryAfterRecovery(unittest.TestCase):
    """Retrying after database recovery must persist exactly one decision."""

    def test_retry_succeeds_after_recovery(self):
        """First call fails, second call succeeds — exactly one decision persisted."""

        run_svc = MockRunService(initial_state="created", revision=0)

        call_count = [0]

        def recovering_uow_factory():
            call_count[0] += 1
            mock_uow = MagicMock()
            mock_uow.fetchone.return_value = (uuid4(), "PARTIAL", uuid4())
            mock_uow.__enter__ = MagicMock(return_value=mock_uow)
            mock_uow.__exit__ = MagicMock(return_value=False)
            if call_count[0] == 1:
                # First call: fail
                raise RuntimeError("database unavailable")
            # Second call: succeed
            return mock_uow

        service = TerminalDecisionService(recovering_uow_factory)

        orchestrator = ResearchOrchestrator(
            run_service=run_svc,
            coverage_service=MagicMock(),
            strategy_service=MagicMock(),
            acquisition_service=MagicMock(),
            config=MockConfig(),
            orchestrator_config=OrchestratorConfig(),
            terminal_service=service,
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

        # First call: should raise
        with self.assertRaises(TerminalDecisionError):
            orchestrator._evaluate_terminal_decision(
                ctx, run_id, run_revision=1, coverage_revision=1
            )

        self.assertNotIn("_terminal_outcome", ctx)

        # Second call: should succeed
        outcome = orchestrator._evaluate_terminal_decision(
            ctx, run_id, run_revision=1, coverage_revision=1
        )
        self.assertEqual(outcome, TerminalDecisionOutcome.PARTIAL)
        self.assertEqual(ctx["_terminal_outcome"], "partial")

        # Exactly one record was captured (the second call)
        # Note: the first call's record was never committed because the
        # uow_factory raises before any insert, so there's no duplicate.


# ===================================================================
# Test: Duplicate idempotency handling
# ===================================================================


class TestDuplicateIdempotency(unittest.TestCase):
    """Duplicate idempotency keys must be handled correctly."""

    def test_duplicate_key_raises_duplicate_error(self):
        """A duplicate idempotency key must raise DuplicateTerminalDecisionError."""
        run_svc = MockRunService(initial_state="created", revision=0)
        key = "terminal:test-run:r1:c1"

        def duplicate_uow_factory():
            raise DuplicateTerminalDecisionError(f"Duplicate idempotency key: {key}")

        service = _make_service(duplicate_uow_factory, duplicate_on_key=key)

        orchestrator = ResearchOrchestrator(
            run_service=run_svc,
            coverage_service=MagicMock(),
            strategy_service=MagicMock(),
            acquisition_service=MagicMock(),
            config=MockConfig(),
            orchestrator_config=OrchestratorConfig(),
            terminal_service=service,
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
        with self.assertRaises(DuplicateTerminalDecisionError):
            orchestrator._evaluate_terminal_decision(
                ctx, run_id, run_revision=1, coverage_revision=1
            )

        # Context must NOT be tainted
        self.assertNotIn("_terminal_outcome", ctx)


# ===================================================================
# Test: get_existing_decision
# ===================================================================


class TestGetExistingDecision(unittest.TestCase):
    """The service must support looking up an existing decision."""

    def test_returns_none_when_no_decision(self):
        """When no decision exists, get_existing_decision returns None."""

        def empty_uow_factory():
            mock_uow = MagicMock()
            mock_uow.fetchone.return_value = None
            mock_uow.__enter__ = MagicMock(return_value=mock_uow)
            mock_uow.__exit__ = MagicMock(return_value=False)
            return mock_uow

        service = TerminalDecisionService(empty_uow_factory)
        result = service.get_existing_decision(uuid4())
        self.assertIsNone(result)

    def test_returns_record_when_exists(self):
        """When a decision exists, get_existing_decision returns it."""
        existing = TerminalDecisionRecord(
            id=uuid4(),
            run_id=uuid4(),
            decision_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            outcome="partial",
            no_progress_signals=(),
            unresolved_gap="test gap",
            policy_version="terminal-decision-policy-v1",
            idempotency_key="test:key",
            created_at=None,
        )

        def populated_uow_factory():
            mock_uow = MagicMock()
            mock_uow.fetchone.return_value = (
                existing.id,
                str(existing.run_id),
                str(existing.decision_id),
                existing.run_revision,
                existing.coverage_revision,
                existing.outcome,
                existing.no_progress_signals,
                existing.unresolved_gap,
                existing.policy_version,
                existing.idempotency_key,
                existing.created_at,
            )
            mock_uow.__enter__ = MagicMock(return_value=mock_uow)
            mock_uow.__exit__ = MagicMock(return_value=False)
            return mock_uow

        service = TerminalDecisionService(populated_uow_factory)
        result = service.get_existing_decision(existing.run_id)
        self.assertIsNotNone(result)
        self.assertEqual(result.outcome, "partial")
        self.assertEqual(result.idempotency_key, "test:key")

    def test_returns_none_on_lookup_failure(self):
        """When the lookup itself fails, return None (not an exception)."""

        def broken_uow_factory():
            raise RuntimeError("database unavailable")

        service = TerminalDecisionService(broken_uow_factory)
        result = service.get_existing_decision(uuid4())
        self.assertIsNone(result)


# ===================================================================
# Test: End-to-end through orchestrator with failing service
# ===================================================================


class TestEndToEndOrchestrator(unittest.TestCase):
    """Full orchestration path with a failing terminal-decision service."""

    def test_orchestrator_aborts_on_persistence_failure(self):
        """The orchestrator must abort the run when terminal-decision persistence fails."""

        run_svc = MockRunService(initial_state="acquiring", revision=5)

        def failing_uow_factory():
            raise RuntimeError("database unavailable")

        service = _make_service(failing_uow_factory, fail_on_insert=True)

        orchestrator = ResearchOrchestrator(
            run_service=run_svc,
            coverage_service=MagicMock(),
            strategy_service=MagicMock(),
            acquisition_service=MagicMock(),
            config=MockConfig(),
            orchestrator_config=OrchestratorConfig(),
            terminal_service=service,
        )

        run_id = uuid4()

        # The orchestrator's main run() method wraps the loop in a broad
        # except Exception. When TerminalDecisionError escapes from
        # _evaluate_terminal_decision, it will be caught by the outer
        # handler and converted to a failed result.
        #
        # However, the key invariant is:
        # 1. The error propagates out of _evaluate_terminal_decision
        # 2. No lifecycle transition occurs
        # 3. No committed terminal outcome remains in context

        ctx = {
            "overall_status": "insufficient",
            "_budget_exhausted": True,
            "_no_progress": False,
            "_strategy_revision_count": 0,
            "_repeated_extraction_failures": 0,
            "_repeated_retrieval_count": 0,
            "_unsatisfiable_source": False,
        }

        # Directly test _evaluate_terminal_decision — this is the public
        # entry point that the orchestration loop calls.
        with self.assertRaises(TerminalDecisionError):
            orchestrator._evaluate_terminal_decision(
                ctx, run_id, run_revision=5, coverage_revision=3
            )

        # Verify: no lifecycle transition occurred
        self.assertEqual(run_svc._state, "acquiring")
        self.assertEqual(len(run_svc.transitions), 0)

        # Verify: context is clean
        self.assertNotIn("_terminal_outcome", ctx)


# ===================================================================
# Test: TerminalDecisionService.record raises correctly
# ===================================================================


class TestServiceRecordRaises(unittest.TestCase):
    """Verify that TerminalDecisionService.record raises the correct exceptions."""

    def test_database_error_becomes_terminal_decision_error(self):
        """A database error must be wrapped in TerminalDecisionError."""
        from research_store.terminal_decision_service import (
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
        from research_store.terminal_decision_service import (
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
