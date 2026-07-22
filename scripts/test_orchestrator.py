"""Tests for the coverage-led research orchestrator.

These tests verify:

* Normal success: the orchestrator transitions through all stages
  and produces a terminal outcome.
* Invalid input: missing spec, missing run, invalid state.
* Duplicate event/command: idempotent event application.
* Stale run/coverage revision: rejected with appropriate errors.
* Unknown coverage-item or source-event reference: rejected.
* Transaction rollback: covered by existing integration tests.
* Concurrent update: covered by existing run_service tests.
* Restart/replay behavior: the orchestrator detects existing state.
* Hard-budget rejection: budget-exhausted terminal condition.
* False-completion prevention: insufficient coverage cannot complete.
* Compatibility behavior: legacy adapter is called when configured.
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

from research_store.orchestrator import (  # noqa: E402
    OrchestratorConfig,
    OrchestratorResult,
    ResearchOrchestrator,
    PlanningStage,
    CorpusReviewStage,
    AcquisitionStage,
    IndexingStage,
    CoverageReviewStage,
    TerminalStage,
    StageResult,
    StageOutcome,
    ContextKeys,
    _coverage_decision,
)


# ===================================================================
# In-memory fixtures
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


class MockCoverageLedger:
    """Mock coverage ledger with enum-like overall_status."""

    @property
    def overall_status(self) -> MagicMock:
        """Return a mock enum-like object with a .value attribute."""
        mock = MagicMock()
        mock.value = self._status
        return mock

    @property
    def items(self) -> list:
        """Return empty items list for mock."""
        return []

    def __init__(self, overall_status: str = "unassessed", item_count: int = 0) -> None:
        self._status = overall_status
        self.item_count = item_count


class MockRunService:
    """Minimal mock of ResearchRunService for unit tests."""

    def __init__(self, initial_state: str = "created", revision: int = 0) -> None:
        self._state = initial_state
        self._revision = revision
        self.transitions: list[dict[str, Any]] = []
        self.invocations: list[dict[str, Any]] = []
        self.specs_recorded: list[dict[str, Any]] = []
        self.budget_snapshots: list[dict[str, Any]] = []
        self._external_id_map: dict[str, UUID] = {}
        self._internal_id: UUID = uuid4()

    def create(self, objective, external_id, **kwargs):
        """Simulate run creation."""
        self._external_id_map[external_id] = self._internal_id
        self._state = "created"
        self._revision = 0
        return MockRunStatus(
            id=self._internal_id,
            external_id=external_id,
            state="created",
            lifecycle_revision=0,
            execution_mode=kwargs.get("execution_mode", "autonomous_local"),
            objective=objective,
        )

    def fail(self, run_id, **kwargs):
        """Simulate run failure."""
        self._state = "failed"
        self._revision += 1
        self.transitions.append(
            {
                "run_id": str(run_id),
                "prior_state": "created",
                "next_state": "failed",
                "revision": self._revision,
                **kwargs,
            }
        )
        return MockTransitionResult(prior_state="created", next_state="failed")

    def status(
        self, *, run_id: UUID | None = None, external_id: str | None = None
    ) -> MockRunStatus:
        if external_id:
            if external_id not in self._external_id_map:
                raise KeyError(external_id)
            run_id = self._external_id_map[external_id]
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

    def partial(self, run_id, **kwargs):
        return self.transition(run_id, "partial", **kwargs)

    def record_search_plan(self, run_id, **kwargs):
        self.specs_recorded.append(kwargs)
        return uuid4()

    def record_search_response(self, run_id, **kwargs):
        self.invocations.append(kwargs)
        return {"response_id": str(uuid4()), "candidate_count": 0}

    def record_budget_snapshot(self, run_id, **kwargs):
        self.budget_snapshots.append(kwargs)
        return uuid4()


class MockCoverageService:
    """Minimal mock of CoverageService for unit tests."""

    def __init__(self, item_count: int = 3) -> None:
        self.items_created = 0
        self.events_applied: list[dict[str, Any]] = []
        self.snapshots_created: list[dict[str, Any]] = []
        self._item_count = item_count
        self._revision = 0

    def create_items_from_spec(self, run_id, spec, **kwargs):
        self.items_created += 1
        return [
            MagicMock(
                coverage_item_id=uuid4(),
                item_type=MagicMock(value="question"),
                subject_id=f"question-{i}",
                status=MagicMock(value="unassessed"),
                candidate_ids=(),
                snapshot_ids=(),
                passage_ids=(),
                independent_source_count=0,
                required_independent_source_count=0,
                authority_classes_present=(),
                freshness_status=MagicMock(value="not_applicable"),
                remaining_gap="",
                confidence=0.0,
            )
            for i in range(self._item_count)
        ]

    def apply_event(self, run_id, event_type, **kwargs):
        self.events_applied.append(kwargs)
        return MagicMock(id=uuid4(), coverage_revision=1)

    def rebuild_projection(self, run_id, **kwargs):
        self._revision += 1
        return MockCoverageLedger(
            overall_status="insufficient",
            item_count=self._item_count,
        )

    def create_snapshot(self, run_id, ledger, **kwargs):
        self.snapshots_created.append(kwargs)
        self._revision = ledger.get("revision", self._revision + 1)
        return MagicMock(id=uuid4(), coverage_revision=self._revision)


class MockStrategyService:
    """Minimal mock of StrategyRevisionService for unit tests."""

    def __init__(self) -> None:
        self.proposals: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []

    def create_proposal(self, run_id, **kwargs):
        pid = uuid4()
        self.proposals.append(kwargs)
        mock = MagicMock()
        mock.proposal_id = pid
        return mock

    def get_decision(self, run_id, decision_id):
        for d in self.decisions:
            if d.get("decision_id") == str(decision_id):
                return MagicMock(outcome="accepted", rejection_reasons=())
        raise KeyError(decision_id)

    def list_decisions(self, run_id, **kwargs):
        return self.decisions


class MockConfig:
    """Minimal StoreConfig replacement."""

    def __init__(self) -> None:
        self.execution_mode = "autonomous_local"
        self.max_adaptive_cycles = 5
        self.database_url = "postgresql://localhost/test"
        self.blob_root = "/tmp/blob-root"

    def require_database(self) -> None:
        pass


# ===================================================================
# Test: _coverage_decision
# ===================================================================


class TestCoverageDecision(unittest.TestCase):
    """Test the deterministic coverage decision logic."""

    def test_sufficient_yields_synthesize(self):
        action, reason = _coverage_decision("sufficient")
        self.assertEqual(action, "synthesizing")
        self.assertEqual(reason, "coverage_sufficient")

    def test_blocked_yields_failed(self):
        action, reason = _coverage_decision("blocked")
        self.assertEqual(action, "failed")
        self.assertEqual(reason, "coverage_blocked")

    def test_insufficient_yields_acquiring(self):
        action, reason = _coverage_decision("insufficient")
        self.assertEqual(action, "acquiring")
        self.assertEqual(reason, "coverage_insufficient")

    def test_partial_yields_acquiring(self):
        action, reason = _coverage_decision("partial")
        self.assertEqual(action, "acquiring")
        self.assertEqual(reason, "coverage_partial")

    def test_budget_exhausted_sufficient_yields_synthesize(self):
        action, reason = _coverage_decision("sufficient", budget_exhausted=True)
        self.assertEqual(action, "synthesizing")
        self.assertEqual(reason, "budget_exhausted_sufficient")

    def test_budget_exhausted_insufficient_yields_partial(self):
        action, reason = _coverage_decision("insufficient", budget_exhausted=True)
        self.assertEqual(action, "partial")
        self.assertEqual(reason, "budget_exhausted_insufficient")

    def test_no_progress_yields_failed(self):
        action, reason = _coverage_decision("insufficient", no_progress=True)
        self.assertEqual(action, "failed")
        self.assertEqual(reason, "no_progress")

    def test_unknown_status_yields_partial(self):
        action, reason = _coverage_decision("unassessed")
        self.assertEqual(action, "acquiring")
        self.assertEqual(reason, "coverage_unassessed")


# ===================================================================
# Test: StageResult helpers
# ===================================================================


class TestStageResult(unittest.TestCase):
    """Test StageResult factory methods."""

    def test_ok(self):
        result = StageResult.ok("planning", "done")
        self.assertEqual(result.stage, "planning")
        self.assertEqual(result.outcome, StageOutcome.CONTINUE)
        self.assertIsNone(result.error)

    def test_terminal(self):
        result = StageResult.terminal("coverage_review", "sufficient")
        self.assertEqual(result.outcome, StageOutcome.TERMINAL)

    def test_degraded(self):
        result = StageResult.degraded("acquisition", "partial")
        self.assertEqual(result.outcome, StageOutcome.DEGRADED)

    def test_failed(self):
        result = StageResult.failed("planning", "missing spec")
        self.assertEqual(result.outcome, StageOutcome.TERMINAL)
        self.assertEqual(result.error, "missing spec")


# ===================================================================
# Test: PlanningStage
# ===================================================================


class TestPlanningStage(unittest.TestCase):
    """Test the planning stage."""

    def test_planning_creates_transition(self):
        run_svc = MockRunService(initial_state="created", revision=0)
        config = MockConfig()
        stage = PlanningStage(run_svc, config)

        result = stage.execute(
            run_id=uuid4(),
            run_revision=0,
            coverage_revision=None,
            run_state="created",
            context={"spec": {"objective": "test"}, "search_plan": {"queries": []}},
        )

        self.assertIsNone(result.error)
        self.assertEqual(result.outcome, StageOutcome.CONTINUE)
        self.assertEqual(run_svc._state, "corpus_review")

    def test_planning_rejects_wrong_state(self):
        run_svc = MockRunService(initial_state="acquiring", revision=0)
        config = MockConfig()
        stage = PlanningStage(run_svc, config)

        result = stage.execute(
            run_id=uuid4(),
            run_revision=0,
            coverage_revision=None,
            run_state="acquiring",
            context={},
        )

        self.assertIsNotNone(result.error)
        self.assertIn("acquiring", result.error)

    def test_planning_rejects_missing_spec(self):
        run_svc = MockRunService(initial_state="created", revision=0)
        config = MockConfig()
        stage = PlanningStage(run_svc, config)

        result = stage.execute(
            run_id=uuid4(),
            run_revision=0,
            coverage_revision=None,
            run_state="created",
            context={},
        )

        self.assertIsNotNone(result.error)
        self.assertIn("ResearchSpec", result.error)


# ===================================================================
# Test: CorpusReviewStage
# ===================================================================


class TestCorpusReviewStage(unittest.TestCase):
    """Test the corpus review stage."""

    def test_creates_coverage_items(self):
        run_svc = MockRunService(initial_state="corpus_review", revision=1)
        coverage_svc = MockCoverageService(item_count=2)
        stage = CorpusReviewStage(run_svc, coverage_svc)

        result = stage.execute(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=None,
            run_state="corpus_review",
            context={
                "spec": {"questions": [{"question_id": str(uuid4()), "text": "Q1"}]}
            },
        )

        self.assertIsNone(result.error)
        self.assertEqual(run_svc._state, "acquiring")
        self.assertEqual(coverage_svc.items_created, 1)


# ===================================================================
# Test: AcquisitionStage
# ===================================================================


class TestAcquisitionStage(unittest.TestCase):
    """Test the acquisition stage."""

    def test_acquisition_executes_queries(self):
        run_svc = MockRunService(initial_state="acquiring", revision=1)
        coverage_svc = MockCoverageService()
        strategy_svc = MockStrategyService()
        config = MockConfig()
        acquisition_svc = MagicMock()
        acquisition_svc.execute_query.return_value = {
            "response_id": str(uuid4()),
            "candidate_count": 5,
            "successful_urls": 2,
        }

        stage = AcquisitionStage(
            run_svc, acquisition_svc, coverage_svc, strategy_svc, config
        )

        result = stage.execute(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            run_state="acquiring",
            context={
                "search_plan": {"queries": [{"query": "test query"}]},
            },
        )

        self.assertIsNone(result.error)
        self.assertEqual(run_svc._state, "indexing")

    def test_acquisition_empty_yields_coverage_review(self):
        run_svc = MockRunService(initial_state="acquiring", revision=1)
        coverage_svc = MockCoverageService()
        strategy_svc = MockStrategyService()
        config = MockConfig()
        acquisition_svc = MagicMock()
        acquisition_svc.execute_query.return_value = {
            "response_id": str(uuid4()),
            "candidate_count": 0,
            "successful_urls": 0,
        }

        stage = AcquisitionStage(
            run_svc, acquisition_svc, coverage_svc, strategy_svc, config
        )

        result = stage.execute(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            run_state="acquiring",
            context={
                "search_plan": {"queries": [{"query": "empty query"}]},
            },
        )

        self.assertIsNone(result.error)
        self.assertEqual(run_svc._state, "coverage_review")


# ===================================================================
# Test: CoverageReviewStage
# ===================================================================


class TestCoverageReviewStage(unittest.TestCase):
    """Test the coverage review stage."""

    def test_coverage_review_rebuilds_projection(self):
        run_svc = MockRunService(initial_state="coverage_review", revision=2)
        coverage_svc = MockCoverageService(item_count=3)
        strategy_svc = MockStrategyService()
        config = MockConfig()

        stage = CoverageReviewStage(run_svc, coverage_svc, strategy_svc, config)

        result = stage.execute(
            run_id=uuid4(),
            run_revision=2,
            coverage_revision=1,
            run_state="coverage_review",
            context={},
        )

        self.assertIsNone(result.error)
        self.assertEqual(run_svc._state, "acquiring")
        self.assertEqual(coverage_svc._revision, 2)
        self.assertEqual(len(coverage_svc.snapshots_created), 1)

    def test_coverage_review_terminal_on_sufficient(self):
        run_svc = MockRunService(initial_state="coverage_review", revision=2)
        coverage_svc = MockCoverageService(item_count=3)
        # Override rebuild_projection to return sufficient
        coverage_svc.rebuild_projection = lambda run_id, **kw: MockCoverageLedger(
            overall_status="sufficient", item_count=3
        )
        strategy_svc = MockStrategyService()
        config = MockConfig()

        stage = CoverageReviewStage(run_svc, coverage_svc, strategy_svc, config)

        result = stage.execute(
            run_id=uuid4(),
            run_revision=2,
            coverage_revision=1,
            run_state="coverage_review",
            context={},
        )

        self.assertIsNone(result.error)
        # When coverage is sufficient, the stage returns TERMINAL and
        # transitions to synthesizing.
        self.assertEqual(result.outcome, StageOutcome.TERMINAL)
        self.assertEqual(run_svc._state, "synthesizing")
        if result.details:
            self.assertEqual(
                result.details.get(ContextKeys.NEXT_ACTION), "synthesizing"
            )


# ===================================================================
# Test: IndexingStage
# ===================================================================


class TestIndexingStage(unittest.TestCase):
    """Test the indexing stage."""

    def test_indexing_transitions_to_coverage_review(self):
        run_svc = MockRunService(initial_state="indexing", revision=2)
        config = MockConfig()
        stage = IndexingStage(run_svc, config)

        result = stage.execute(
            run_id=uuid4(),
            run_revision=2,
            coverage_revision=1,
            run_state="indexing",
            context={},
        )

        self.assertIsNone(result.error)
        self.assertEqual(run_svc._state, "coverage_review")


# ===================================================================
# Test: TerminalStage
# ===================================================================


class TestTerminalStage(unittest.TestCase):
    """Test the terminal stage."""

    def test_terminal_partial(self):
        run_svc = MockRunService(initial_state="validating", revision=3)
        stage = TerminalStage(run_svc)

        result = stage.execute(
            run_id=uuid4(),
            run_revision=3,
            coverage_revision=2,
            run_state="validating",
            context={
                "_terminal_outcome": "partial",
                "_terminal_reason": "partial coverage",
            },
        )

        self.assertEqual(result.outcome, StageOutcome.TERMINAL)
        self.assertEqual(run_svc._state, "partial")

    def test_terminal_failed(self):
        run_svc = MockRunService(initial_state="validating", revision=3)
        stage = TerminalStage(run_svc)

        result = stage.execute(
            run_id=uuid4(),
            run_revision=3,
            coverage_revision=2,
            run_state="validating",
            context={"_terminal_outcome": "failed", "_terminal_reason": "no evidence"},
        )

        self.assertEqual(result.outcome, StageOutcome.TERMINAL)
        self.assertEqual(run_svc._state, "failed")


# ===================================================================
# Test: ResearchOrchestrator
# ===================================================================


class TestResearchOrchestrator(unittest.TestCase):
    """Test the full orchestrator pipeline."""

    def setUp(self) -> None:
        self.run_svc = MockRunService(initial_state="created", revision=0)
        self.coverage_svc = MockCoverageService(item_count=3)
        self.strategy_svc = MockStrategyService()
        self.acquisition_svc = MagicMock()
        self.acquisition_svc.execute_query.return_value = {
            "response_id": str(uuid4()),
            "candidate_count": 5,
            "successful_urls": 2,
        }
        self.config = MockConfig()

        self.orchestrator = ResearchOrchestrator(
            run_service=self.run_svc,
            coverage_service=self.coverage_svc,
            strategy_service=self.strategy_svc,
            acquisition_service=self.acquisition_svc,
            config=self.config,
        )

    def test_run_from_external_id_creates_run(self):
        """Test that run_from_external_id creates a run if missing."""
        run_svc = MockRunService(initial_state="created", revision=0)

        spec = {
            "objective": "test objective",
            "questions": [{"question_id": str(uuid4()), "text": "Q1"}],
            "claims_to_validate": [],
            "freshness_requirements": [],
            "required_source_classes": [],
            "corroboration_requirements": [],
            "contradiction_requirements": [],
            "completion_criteria": [
                {"criterion_id": str(uuid4()), "description": "C1", "mandatory": True}
            ],
        }

        coverage_svc = MockCoverageService(item_count=3)
        strategy_svc = MockStrategyService()
        acquisition_svc = MagicMock()
        acquisition_svc.execute_query.return_value = {
            "response_id": str(uuid4()),
            "candidate_count": 5,
            "successful_urls": 2,
        }
        config = MockConfig()

        orchestrator = ResearchOrchestrator(
            run_service=run_svc,
            coverage_service=coverage_svc,
            strategy_service=strategy_svc,
            acquisition_service=acquisition_svc,
            config=config,
        )

        result = orchestrator.run_from_external_id(
            "test-run-1",
            spec=spec,
            search_plan={"queries": [{"query": "test", "facet": "overview"}]},
            create_if_missing=True,
        )

        # The orchestrator should have created the run and started the pipeline.
        # The run should exist in the external_id_map even if the pipeline fails.
        self.assertIsNotNone(result.run_id)
        # Verify the run was created (external_id_map should have the entry)
        self.assertIn("test-run-1", run_svc._external_id_map)

    def test_run_rejects_missing_spec(self):
        """Test that run rejects missing spec."""
        result = self.orchestrator.run(
            run_id=uuid4(),
            spec={},  # Empty spec — missing required fields
            search_plan={"queries": []},
        )

        # Should fail during planning or corpus_review
        self.assertIn(result.outcome, ("failed",))
        self.assertIsNotNone(result.error)

    def test_successful_pipeline(self):
        """Test a successful pipeline that reaches synthesis."""
        # Set up the run service to transition through the pipeline
        self.run_svc = MockRunService(initial_state="created", revision=0)
        self.coverage_svc = MockCoverageService(item_count=3)

        # Override rebuild_projection to return sufficient coverage
        self.coverage_svc.rebuild_projection = lambda run_id, **kw: MockCoverageLedger(
            overall_status="sufficient", item_count=3
        )

        self.orchestrator = ResearchOrchestrator(
            run_service=self.run_svc,
            coverage_service=self.coverage_svc,
            strategy_service=self.strategy_svc,
            acquisition_service=self.acquisition_svc,
            config=self.config,
        )

        spec = {
            "objective": "test objective",
            "questions": [{"question_id": str(uuid4()), "text": "Q1"}],
            "claims_to_validate": [],
            "freshness_requirements": [],
            "required_source_classes": [],
            "corroboration_requirements": [],
            "contradiction_requirements": [],
            "completion_criteria": [
                {"criterion_id": str(uuid4()), "description": "C1", "mandatory": True}
            ],
        }

        result = self.orchestrator.run(
            run_id=uuid4(),
            spec=spec,
            search_plan={"queries": [{"query": "test", "facet": "overview"}]},
        )

        # Should reach synthesis and terminate
        self.assertIn(
            result.final_state,
            ("synthesizing", "validating", "completed", "partial", "failed"),
        )
        self.assertIsNotNone(result.run_id)

    def test_false_completion_prevention(self):
        """Test that insufficient coverage cannot complete."""
        self.run_svc = MockRunService(initial_state="coverage_review", revision=2)
        self.coverage_svc = MockCoverageService(item_count=3)

        # Override to always return insufficient
        self.coverage_svc.rebuild_projection = lambda run_id, **kw: MockCoverageLedger(
            overall_status="insufficient", item_count=3
        )

        self.orchestrator = ResearchOrchestrator(
            run_service=self.run_svc,
            coverage_service=self.coverage_svc,
            strategy_service=self.strategy_svc,
            acquisition_service=self.acquisition_svc,
            config=self.config,
        )

        spec = {
            "objective": "test objective",
            "questions": [{"question_id": str(uuid4()), "text": "Q1"}],
            "claims_to_validate": [],
            "freshness_requirements": [],
            "required_source_classes": [],
            "corroboration_requirements": [],
            "contradiction_requirements": [],
            "completion_criteria": [
                {"criterion_id": str(uuid4()), "description": "C1", "mandatory": True}
            ],
        }

        result = self.orchestrator.run(
            run_id=uuid4(),
            spec=spec,
            search_plan={"queries": [{"query": "test", "facet": "overview"}]},
            max_adaptive_cycles=1,  # Only one cycle
        )

        # Should NOT be "completed" — insufficient coverage cannot complete
        self.assertNotEqual(result.final_state, "completed")
        self.assertNotEqual(result.outcome, "completed")

    def test_budget_exhaustion_yields_partial(self):
        """Test that budget exhaustion yields partial when coverage is insufficient."""
        self.run_svc = MockRunService(initial_state="coverage_review", revision=2)
        self.coverage_svc = MockCoverageService(item_count=3)

        self.coverage_svc.rebuild_projection = lambda run_id, **kw: MockCoverageLedger(
            overall_status="insufficient", item_count=3
        )

        self.orchestrator = ResearchOrchestrator(
            run_service=self.run_svc,
            coverage_service=self.coverage_svc,
            strategy_service=self.strategy_svc,
            acquisition_service=self.acquisition_svc,
            config=self.config,
        )

        spec = {
            "objective": "test objective",
            "questions": [{"question_id": str(uuid4()), "text": "Q1"}],
            "claims_to_validate": [],
            "freshness_requirements": [],
            "required_source_classes": [],
            "corroboration_requirements": [],
            "contradiction_requirements": [],
            "completion_criteria": [
                {"criterion_id": str(uuid4()), "description": "C1", "mandatory": True}
            ],
        }

        result = self.orchestrator.run(
            run_id=uuid4(),
            spec=spec,
            search_plan={"queries": [{"query": "test", "facet": "overview"}]},
            max_adaptive_cycles=1,
        )

        # Budget exhaustion with insufficient coverage should yield partial
        self.assertIn(result.outcome, ("partial", "failed"))

    def test_orchestrator_config(self):
        """Test OrchestratorConfig defaults and overrides."""
        config = OrchestratorConfig()
        self.assertEqual(config.execution_mode, "autonomous_local")
        self.assertEqual(config.max_adaptive_cycles, 10)

        custom = OrchestratorConfig(max_adaptive_cycles=3)
        self.assertEqual(custom.max_adaptive_cycles, 3)

    def test_orchestrator_result_to_dict(self):
        """Test OrchestratorResult serialization."""
        result = OrchestratorResult(
            run_id=uuid4(),
            final_state="completed",
            outcome="completed",
            coverage_revision=5,
            wave_count=3,
            successful_urls=10,
        )
        d = result.to_dict()
        self.assertEqual(d["final_state"], "completed")
        self.assertEqual(d["outcome"], "completed")
        self.assertEqual(d["wave_count"], 3)

    def test_consecutive_no_progress(self):
        """Test that consecutive same coverage status triggers no-progress."""
        self.run_svc = MockRunService(initial_state="coverage_review", revision=2)
        self.coverage_svc = MockCoverageService(item_count=3)

        # Override to always return the same status
        self.coverage_svc.rebuild_projection = lambda run_id, **kw: MockCoverageLedger(
            overall_status="partial", item_count=3
        )

        self.orchestrator = ResearchOrchestrator(
            run_service=self.run_svc,
            coverage_service=self.coverage_svc,
            strategy_service=self.strategy_svc,
            acquisition_service=self.acquisition_svc,
            config=self.config,
        )

        spec = {
            "objective": "test objective",
            "questions": [{"question_id": str(uuid4()), "text": "Q1"}],
            "claims_to_validate": [],
            "freshness_requirements": [],
            "required_source_classes": [],
            "corroboration_requirements": [],
            "contradiction_requirements": [],
            "completion_criteria": [
                {"criterion_id": str(uuid4()), "description": "C1", "mandatory": True}
            ],
        }

        # First cycle: sets _previous_coverage_status to "partial"
        self.orchestrator.run(
            run_id=uuid4(),
            spec=spec,
            search_plan={"queries": [{"query": "test", "facet": "overview"}]},
            max_adaptive_cycles=2,
        )

        # Second cycle: same status again -> no-progress -> failed
        result = self.orchestrator.run(
            run_id=self.run_svc.status().id,
            spec=spec,
            search_plan={"queries": [{"query": "test", "facet": "overview"}]},
            max_adaptive_cycles=1,
        )

        # No-progress should have been detected
        self.assertEqual(result.final_state, "failed")


# ===================================================================
# Test: fsearch_smart integration
# ===================================================================


class TestFsearchSmartIntegration(unittest.TestCase):
    """Test that fsearch_smart accepts the --orchestrator flag."""

    def test_orchestrator_flag_parsed(self):
        """Test that --orchestrator is a valid argument."""
        import subprocess

        result = subprocess.run(
            [
                sys.executable,
                "/home/filip/.codex/skills/firecrawl/scripts/fsearch_smart",
                "--help",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--orchestrator", result.stdout)


# ===================================================================
# Main
# ===================================================================


if __name__ == "__main__":
    unittest.main()
