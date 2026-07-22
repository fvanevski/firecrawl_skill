"""Comprehensive tests for strategy-revision proposal and authorization.

Tests cover:

* Proposal creation and retrieval (normal success).
* Stale revision rejection (stale run revision, stale coverage revision).
* Budget rejection (exceeding effective hard limits).
* Scope expansion validation (rationale required, unjustified expansion).
* Duplicate action checks (query, candidate, retrieval).
* Terminal run state rejection.
* Unknown decision type rejection.
* Missing target items and rationale.
* Accepted proposal recording.
* Rejected proposal auditing.
* Idempotency (duplicate proposal/decision keys).
* Validation-only (no persistence).
* Domain model validation (StrategyRevisionDecision invariants).
* Rejection reason taxonomy.

All tests are unit tests using an in-memory repository — no PostgreSQL required.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from research_domain.models import (
    RejectionReason,
    ScopeExpansionRationale,
    ScopeExpansionType,
    StrategyDecision,
    StrategyRevisionDecision,
)
from budget_policy import BudgetPolicy, BudgetSnapshot, ResourceCaps
from research_store.strategy_service import (
    DecisionNotFoundError,
    ProposalNotFoundError,
    StrategyRevisionService,
    StrategyServiceError,
)
from research_store.strategy_validator import (
    StrategyRevisionValidator,
    ValidationResult,
)


# ---------------------------------------------------------------------------
# Memory repository fixture
# ---------------------------------------------------------------------------


class MemoryStrategyRepository:
    """In-memory strategy-revision repository for unit tests."""

    def __init__(self):
        self.proposals: dict[str, dict] = {}  # (run_id, proposal_id) -> mapping
        self.decisions: dict[str, dict] = {}  # (run_id, decision_id) -> mapping
        self.idempotency_proposals: dict[str, dict] = {}  # (run_id, idempotency_key)
        self.idempotency_decisions: dict[str, dict] = {}
        self._next_order_counters: dict[str, int] = {}  # run_id -> next revision_order

    # Context manager support
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
        return False

    def commit(self):
        pass

    def rollback(self):
        pass

    def _get_next_order(self, run_id: str) -> int:
        current = self._next_order_counters.get(run_id, 0)
        self._next_order_counters[run_id] = current + 1
        return self._next_order_counters[run_id]

    def record_proposal(
        self,
        run_id,
        proposal_id,
        run_revision,
        coverage_revision,
        decision_type,
        target_coverage_item_ids,
        proposed_queries,
        proposed_candidate_ids,
        proposed_retrieval_queries,
        expected_contribution,
        estimated_cost,
        rationale,
        confidence,
        idempotency_key,
        **metadata,
    ):
        run_id_str = str(run_id)
        proposal_id_str = str(proposal_id)
        idem_key = (run_id_str, idempotency_key)

        # Idempotency check
        if idem_key in self.idempotency_proposals:
            return self.idempotency_proposals[idem_key]["id"]

        order = self._get_next_order(run_id_str)
        mapping = {
            "id": str(uuid4()),
            "run_id": run_id_str,
            "run_revision": run_revision,
            "coverage_revision": coverage_revision,
            "revision_order": order,
            "row_type": "proposal",
            "proposal_id": proposal_id_str,
            "decision_type": decision_type,
            "target_coverage_item_ids": target_coverage_item_ids,
            "proposed_queries": proposed_queries,
            "proposed_candidate_ids": proposed_candidate_ids,
            "proposed_retrieval_queries": proposed_retrieval_queries,
            "expected_contribution": expected_contribution,
            "estimated_cost": estimated_cost,
            "rationale": rationale,
            "confidence": confidence,
            "idempotency_key": idempotency_key,
            "actor_type": metadata.get("actor_type", "system"),
            "actor_identifier": metadata.get("actor_identifier"),
            "created_at": None,
        }
        self.proposals[(run_id_str, proposal_id_str)] = mapping
        self.idempotency_proposals[idem_key] = mapping
        return mapping["id"]

    def get_proposal(self, run_id, proposal_id):
        run_id_str, proposal_id_str = str(run_id), str(proposal_id)
        return self.proposals.get((run_id_str, proposal_id_str))

    def list_proposals(
        self, run_id, *, run_revision=None, coverage_revision=None, limit=100, offset=0
    ):
        run_id_str = str(run_id)
        results = []
        for (rid, _pid), m in self.proposals.items():
            if rid != run_id_str or m["row_type"] != "proposal":
                continue
            if run_revision is not None and m["run_revision"] != run_revision:
                continue
            if (
                coverage_revision is not None
                and m["coverage_revision"] != coverage_revision
            ):
                continue
            results.append(m)
        results.sort(key=lambda m: m["revision_order"], reverse=True)
        return results[offset : offset + limit]

    def record_decision(
        self,
        run_id,
        decision_id,
        proposal_id,
        run_revision,
        coverage_revision,
        outcome,
        rejection_reasons,
        policy_version,
        scope_expansion_type,
        scope_expansion_rationale,
        scope_expansion_approved,
        authorized_by,
        idempotency_key,
        **metadata,
    ):
        run_id_str = str(run_id)
        decision_id_str = str(decision_id)
        idem_key = (run_id_str, idempotency_key)

        if idem_key in self.idempotency_decisions:
            return self.idempotency_decisions[idem_key]["id"]

        order = self._get_next_order(run_id_str)
        mapping = {
            "id": str(uuid4()),
            "run_id": run_id_str,
            "run_revision": run_revision,
            "coverage_revision": coverage_revision,
            "revision_order": order,
            "row_type": "decision",
            "proposal_id": str(proposal_id),
            "decision_id": decision_id_str,
            "outcome": outcome,
            "rejection_reasons": rejection_reasons or [],
            "policy_version": policy_version,
            "scope_expansion_type": scope_expansion_type,
            "scope_expansion_rationale": scope_expansion_rationale,
            "scope_expansion_approved": scope_expansion_approved,
            "authorized_by": authorized_by,
            "idempotency_key": idempotency_key,
            "actor_type": metadata.get("actor_type", "system"),
            "actor_identifier": metadata.get("actor_identifier"),
            "created_at": None,
        }
        self.decisions[(run_id_str, decision_id_str)] = mapping
        self.idempotency_decisions[idem_key] = mapping
        return mapping["id"]

    def get_decision(self, run_id, decision_id):
        run_id_str, decision_id_str = str(run_id), str(decision_id)
        return self.decisions.get((run_id_str, decision_id_str))

    def list_decisions(
        self, run_id, *, proposal_id=None, outcome=None, limit=100, offset=0
    ):
        run_id_str = str(run_id)
        results = []
        for (rid, _did), m in self.decisions.items():
            if rid != run_id_str or m["row_type"] != "decision":
                continue
            if proposal_id is not None and m["proposal_id"] != str(proposal_id):
                continue
            if outcome is not None and m["outcome"] != outcome:
                continue
            results.append(m)
        results.sort(key=lambda m: m["revision_order"], reverse=True)
        return results[offset : offset + limit]

    def proposal_exists(self, run_id, proposal_id):
        return (str(run_id), str(proposal_id)) in self.proposals

    def get_proposal_by_idempotency(self, run_id, idempotency_key):
        run_id_str = str(run_id)
        key = (run_id_str, idempotency_key)
        return self.idempotency_proposals.get(key)

    def decision_exists(self, run_id, decision_id):
        return (str(run_id), str(decision_id)) in self.decisions

    def list_proposal_ids_for_run(self, run_id):
        run_id_str = str(run_id)
        return [
            m["proposal_id"]
            for (rid, _pid), m in self.proposals.items()
            if rid == run_id_str and m["row_type"] == "proposal"
        ]

    def list_decision_ids_for_proposal(self, run_id, proposal_id):
        run_id_str, proposal_id_str = str(run_id), str(proposal_id)
        return [
            m["decision_id"]
            for (rid, _pid), m in self.decisions.items()
            if rid == run_id_str
            and m["proposal_id"] == proposal_id_str
            and m["row_type"] == "decision"
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_budget_snapshot() -> BudgetSnapshot:
    """Create a minimal budget snapshot for testing."""
    caps = ResourceCaps(
        max_search_branches=5,
        results_per_branch=20,
        max_extraction_attempts=10,
        max_successful_extractions=5,
        max_adaptive_cycles=3,
        max_llm_calls=20,
        max_input_tokens=100000,
        max_output_tokens=50000,
        max_retrieval_candidates=50,
        max_reranker_candidates=20,
        max_evidence_packet_tokens=10000,
        max_wall_clock_seconds=3600,
    )
    return BudgetSnapshot(
        snapshot_version=1,
        policy_version="budget-policy-v1",
        policy_config_sha256="abc123",
        research_spec_id=str(uuid4()),
        spec_revision=1,
        run_revision=1,
        selected_tier="focused",
        semantic_inputs={},
        matched_rules=(),
        policy_caps=caps,
        user_limits={},
        effective_caps=caps,
    )


def _make_policy() -> BudgetPolicy:
    return BudgetPolicy.load()


class MemoryStrategyUow:
    def __init__(self, repository):
        self.strategy_revisions = repository
        self.runs = self  # For run_exists check (delegates to strategy_revisions)
        self.coverage = self  # For coverage_items_exist check
        self.commit = lambda: None
        self.rollback = lambda: None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
        return False

    def get_run_status(self, *, run_id=None, external_id=None):
        """Mock implementation for run_exists check."""
        # Always return a valid status for tests
        return {
            "id": str(run_id) if run_id else "",
            "external_id": external_id or "",
            "state": "coverage_review",
            "lifecycle_revision": 1,
            "reopened_from_revision": 0,
            "execution_mode": "autonomous_local",
            "objective": "",
            "declared_outcome": "",
            "status": "active",
            "completed_at": None,
            "error": None,
            "current_coverage_revision": 1,
        }

    def count_coverage_items(self, run_id):
        """Mock implementation for coverage_items_exist check."""
        # Return 1 to indicate coverage items exist
        return 1


def strategy_fixture():
    repo = MemoryStrategyRepository()
    service = StrategyRevisionService(
        lambda: MemoryStrategyUow(repo),
        budget_policy=_make_policy(),
    )
    return repo, service


# ---------------------------------------------------------------------------
# Tests — Validator
# ---------------------------------------------------------------------------


class TestStrategyRevisionValidator:
    """Tests for the deterministic validator."""

    def setup_method(self):
        self.policy = _make_policy()
        self.validator = StrategyRevisionValidator(self.policy)

    # -- Normal success --

    def test_valid_search_proposal(self):
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4(), uuid4()],
            proposed_queries=[{"query": "test query", "facet": ""}],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={"max_search_branches": 1, "max_llm_calls": 0},
            rationale="Test rationale",
            scope_expansion=None,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
        )
        assert result.valid is True
        assert len(result.rejection_reasons) == 0

    def test_valid_stop_partial_proposal(self):
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="stop_partial",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={},
            rationale="",
            scope_expansion=None,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
        )
        assert result.valid is True

    # -- Stale run revision --

    def test_stale_run_revision_rejected(self):
        """Proposal at revision 1 is rejected when current run revision is 0."""
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={},
            rationale="test",
            scope_expansion=None,
            current_run_revision=0,  # current < proposal revision
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
        )
        assert result.valid is False
        assert RejectionReason.STALE_RUN_REVISION in result.rejection_reasons

    def test_run_revision_not_stale_when_equal(self):
        """Proposal at revision 3 is valid when current run revision is 3."""
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=3,
            coverage_revision=3,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={},
            rationale="test",
            scope_expansion=None,
            current_run_revision=3,  # equal — not stale
            current_coverage_revision=3,
            run_state="coverage_review",
            is_terminal=False,
        )
        assert result.valid is True
        assert RejectionReason.STALE_RUN_REVISION not in result.rejection_reasons

    def test_run_revision_not_stale_when_current_is_newer(self):
        """Proposal at revision 1 is valid when current run revision is 5."""
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={},
            rationale="test",
            scope_expansion=None,
            current_run_revision=5,  # newer — not stale
            current_coverage_revision=5,
            run_state="coverage_review",
            is_terminal=False,
        )
        assert result.valid is True
        assert RejectionReason.STALE_RUN_REVISION not in result.rejection_reasons

    # -- Unknown run --

    def test_unknown_run_rejected(self):
        """Proposal is rejected when the run does not exist."""
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={},
            rationale="test",
            scope_expansion=None,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
            run_exists=False,
        )
        assert result.valid is False
        assert RejectionReason.UNKNOWN_RUN in result.rejection_reasons

    def test_unknown_run_not_rejected_when_not_supplied(self):
        """When run_exists is not supplied, UNKNOWN_RUN is not triggered."""
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={},
            rationale="test",
            scope_expansion=None,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
            # run_exists not supplied — should not trigger
        )
        assert result.valid is True
        assert RejectionReason.UNKNOWN_RUN not in result.rejection_reasons

    # -- Unknown coverage items --

    def test_unknown_coverage_items_rejected(self):
        """Proposal is rejected when coverage items do not exist."""
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={},
            rationale="test",
            scope_expansion=None,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
            coverage_items_exist=False,
        )
        assert result.valid is False
        assert RejectionReason.UNKNOWN_COVERAGE_ITEM in result.rejection_reasons

    def test_unknown_coverage_items_not_rejected_when_not_supplied(self):
        """When coverage_items_exist is not supplied, UNKNOWN_COVERAGE_ITEM is not triggered."""
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={},
            rationale="test",
            scope_expansion=None,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
            # coverage_items_exist not supplied — should not trigger
        )
        assert result.valid is True
        assert RejectionReason.UNKNOWN_COVERAGE_ITEM not in result.rejection_reasons

    # -- Stale coverage revision --

    def test_stale_coverage_revision_rejected(self):
        """Proposal at coverage revision 1 is rejected when current is 0."""
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={},
            rationale="test",
            scope_expansion=None,
            current_run_revision=1,
            current_coverage_revision=0,  # current < proposal revision
            run_state="coverage_review",
            is_terminal=False,
        )
        assert result.valid is False
        assert RejectionReason.STALE_COVERAGE_REVISION in result.rejection_reasons

    # -- Terminal run state --

    def test_terminal_run_state_rejected(self):
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={},
            rationale="test",
            scope_expansion=None,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="completed",
            is_terminal=True,
        )
        assert result.valid is False
        assert RejectionReason.TERMINAL_RUN_STATE in result.rejection_reasons

    # -- Unknown decision type --

    def test_unknown_decision_type_rejected(self):
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="unknown_action",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={},
            rationale="test",
            scope_expansion=None,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
        )
        assert result.valid is False
        assert RejectionReason.UNKNOWN_DECISION_TYPE in result.rejection_reasons

    # -- Missing target items --

    def test_missing_target_items_rejected(self):
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[],
            proposed_queries=[],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={},
            rationale="test",
            scope_expansion=None,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
        )
        assert result.valid is False
        assert RejectionReason.MISSING_TARGET_ITEMS in result.rejection_reasons

    # -- Budget exceeded --

    def test_budget_exceeded_rejected(self):
        snapshot = _make_budget_snapshot()
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={"max_search_branches": 999, "max_llm_calls": 999},
            rationale="test",
            scope_expansion=None,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
            budget_snapshot=snapshot,
        )
        assert result.valid is False
        assert RejectionReason.BUDGET_EXCEEDED in result.rejection_reasons

    # -- Duplicate queries --

    def test_duplicate_query_rejected(self):
        sigs = ["already searched query"]
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[{"query": "already searched query", "facet": ""}],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={},
            rationale="test",
            scope_expansion=None,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
            existing_query_signatures=sigs,
        )
        assert result.valid is False
        assert RejectionReason.DUPLICATE_ACTION in result.rejection_reasons

    # -- Duplicate candidates --

    def test_duplicate_candidate_rejected(self):
        existing = {uuid4()}
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="scrape",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            proposed_candidate_ids=list(existing),
            proposed_retrieval_queries=[],
            estimated_cost={},
            rationale="test",
            scope_expansion=None,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
            existing_candidate_ids=existing,
        )
        assert result.valid is False
        assert RejectionReason.DUPLICATE_ACTION in result.rejection_reasons

    # -- Duplicate retrieval queries --

    def test_duplicate_retrieval_rejected(self):
        existing = {"retrieval query already used"}
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="retrieve",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=["retrieval query already used"],
            estimated_cost={},
            rationale="test",
            scope_expansion=None,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
            existing_retrieval_queries=existing,
        )
        assert result.valid is False
        assert RejectionReason.DUPLICATE_ACTION in result.rejection_reasons

    # -- Scope expansion --

    def test_scope_expansion_unjustified_rejected(self):
        expansion = ScopeExpansionRationale(
            expansion_type=ScopeExpansionType.NEW_ENTITIES,
            rationale="Need to search broader",
            approved=False,
        )
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[{"query": "broader query", "facet": ""}],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={},
            rationale="test",
            scope_expansion=expansion,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
        )
        assert result.valid is False
        assert RejectionReason.SCOPE_EXPANSION_UNJUSTIFIED in result.rejection_reasons

    # -- Multiple rejection reasons --

    def test_multiple_rejection_reasons(self):
        result = self.validator.validate(
            run_id=uuid4(),
            run_revision=5,  # proposal at revision 5
            coverage_revision=5,
            decision_type="unknown_action",
            target_coverage_item_ids=[],
            proposed_queries=[],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={},
            rationale="",
            scope_expansion=None,
            current_run_revision=3,  # 3 < 5 — stale
            current_coverage_revision=3,  # 3 < 5 — stale
            run_state="completed",
            is_terminal=True,
        )
        assert result.valid is False
        reasons = set(r.value for r in result.rejection_reasons)
        assert "terminal_run_state" in reasons
        assert "unknown_decision_type" in reasons
        assert "missing_target_items" in reasons
        assert "stale_run_revision" in reasons
        assert "stale_coverage_revision" in reasons


# ---------------------------------------------------------------------------
# Tests — Service (memory repo)
# ---------------------------------------------------------------------------


class TestStrategyRevisionService:
    """Tests for StrategyRevisionService with in-memory repository."""

    def setup_method(self):
        self.repo, self.service = strategy_fixture()

    # -- Proposal creation --

    def test_create_proposal_success(self):
        run_id = uuid4()
        target_ids = [uuid4(), uuid4()]
        proposal = self.service.create_proposal(
            run_id=run_id,
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=target_ids,
            proposed_queries=[{"query": "test query", "facet": ""}],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=["retrieval q"],
            expected_contribution="Find evidence for claims",
            estimated_cost={"max_search_branches": 1, "max_llm_calls": 2},
            rationale="Need to search for X",
            confidence=0.8,
        )
        assert proposal.proposal_id is not None
        assert proposal.run_revision == 1
        assert proposal.coverage_revision == 1
        assert proposal.decision == StrategyDecision("search")
        assert len(proposal.target_coverage_item_ids) == 2
        assert proposal.rationale == "Need to search for X"
        assert proposal.confidence == 0.8

    def test_create_proposal_requires_run_id(self):
        with pytest.raises(StrategyServiceError, match="run_id is required"):
            self.service.create_proposal(
                run_id=None,  # type: ignore
                run_revision=1,
                coverage_revision=1,
                decision_type="search",
                target_coverage_item_ids=[uuid4()],
                proposed_queries=[],
                rationale="test",
            )

    def test_create_proposal_requires_revision(self):
        with pytest.raises(StrategyServiceError, match="run_revision must be >= 1"):
            self.service.create_proposal(
                run_id=uuid4(),
                run_revision=0,
                coverage_revision=1,
                decision_type="search",
                target_coverage_item_ids=[uuid4()],
                proposed_queries=[],
                rationale="test",
            )

    def test_create_proposal_requires_target_items(self):
        with pytest.raises(
            StrategyServiceError, match="target_coverage_item_ids is required"
        ):
            self.service.create_proposal(
                run_id=uuid4(),
                run_revision=1,
                coverage_revision=1,
                decision_type="search",
                target_coverage_item_ids=[],
                proposed_queries=[],
                rationale="test",
            )

    # -- Proposal retrieval --

    def test_get_proposal(self):
        run_id = uuid4()
        proposal = self.service.create_proposal(
            run_id=run_id,
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            rationale="test",
        )
        retrieved = self.service.get_proposal(run_id, proposal.proposal_id)
        assert retrieved.proposal_id == proposal.proposal_id
        assert retrieved.rationale == "test"

    def test_get_proposal_not_found(self):
        with pytest.raises(ProposalNotFoundError):
            self.service.get_proposal(uuid4(), uuid4())

    # -- List proposals --

    def test_list_proposals(self):
        run_id = uuid4()
        self.service.create_proposal(
            run_id=run_id,
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            rationale="first",
        )
        self.service.create_proposal(
            run_id=run_id,
            run_revision=2,
            coverage_revision=2,
            decision_type="scrape",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            rationale="second",
        )
        proposals = self.service.list_proposals(run_id)
        assert len(proposals) == 2

    # -- Authorization: accepted --

    def test_authorize_accepted(self):
        run_id = uuid4()
        proposal = self.service.create_proposal(
            run_id=run_id,
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[{"query": "test query", "facet": ""}],
            proposed_candidate_ids=[],
            proposed_retrieval_queries=[],
            estimated_cost={"max_search_branches": 1},
            rationale="Need to search",
            confidence=0.9,
        )
        decision = self.service.authorize(
            run_id=run_id,
            proposal_id=proposal.proposal_id,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
            budget_snapshot=_make_budget_snapshot(),
        )
        assert decision.outcome == "accepted"
        assert len(decision.rejection_reasons) == 0

    # -- Authorization: rejected (stale) --

    def test_authorize_rejected_stale(self):
        run_id = uuid4()
        proposal = self.service.create_proposal(
            run_id=run_id,
            run_revision=5,
            coverage_revision=5,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            rationale="test",
        )
        decision = self.service.authorize(
            run_id=run_id,
            proposal_id=proposal.proposal_id,
            current_run_revision=3,  # 3 < 5 — stale
            current_coverage_revision=3,  # 3 < 5 — stale
            run_state="coverage_review",
            is_terminal=False,
        )
        assert decision.outcome == "rejected"
        assert len(decision.rejection_reasons) > 0

    # -- Authorization: rejected (terminal) --

    def test_authorize_rejected_terminal(self):
        run_id = uuid4()
        proposal = self.service.create_proposal(
            run_id=run_id,
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            rationale="test",
        )
        decision = self.service.authorize(
            run_id=run_id,
            proposal_id=proposal.proposal_id,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="completed",
            is_terminal=True,
        )
        assert decision.outcome == "rejected"
        reasons = set(r.value for r in decision.rejection_reasons)
        assert "terminal_run_state" in reasons

    # -- Authorization: rejected (budget) --

    def test_authorize_rejected_budget(self):
        run_id = uuid4()
        proposal = self.service.create_proposal(
            run_id=run_id,
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            estimated_cost={"max_search_branches": 999},
            rationale="test",
        )
        decision = self.service.authorize(
            run_id=run_id,
            proposal_id=proposal.proposal_id,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
            budget_snapshot=_make_budget_snapshot(),
        )
        assert decision.outcome == "rejected"
        assert RejectionReason.BUDGET_EXCEEDED in decision.rejection_reasons

    # -- Decision retrieval --

    def test_get_decision(self):
        run_id = uuid4()
        proposal = self.service.create_proposal(
            run_id=run_id,
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            rationale="test",
        )
        decision = self.service.authorize(
            run_id=run_id,
            proposal_id=proposal.proposal_id,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
        )
        retrieved = self.service.get_decision(run_id, decision.decision_id)
        assert retrieved.decision_id == decision.decision_id
        assert retrieved.outcome == "accepted"

    def test_get_decision_not_found(self):
        with pytest.raises(DecisionNotFoundError):
            self.service.get_decision(uuid4(), uuid4())

    # -- List decisions --

    def test_list_decisions(self):
        run_id = uuid4()
        p1 = self.service.create_proposal(
            run_id=run_id,
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            rationale="test",
        )
        self.service.authorize(
            run_id=run_id,
            proposal_id=p1.proposal_id,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
        )
        decisions = self.service.list_decisions(run_id)
        assert len(decisions) == 1

    def test_list_decisions_filtered_by_outcome(self):
        run_id = uuid4()
        p1 = self.service.create_proposal(
            run_id=run_id,
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            rationale="test",
        )
        self.service.authorize(
            run_id=run_id,
            proposal_id=p1.proposal_id,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
        )
        accepted = self.service.list_decisions(run_id, outcome="accepted")
        assert len(accepted) == 1
        rejected = self.service.list_decisions(run_id, outcome="rejected")
        assert len(rejected) == 0

    # -- Validation-only --

    def test_validate_proposal_accepted(self):
        result = self.service.validate_proposal(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[{"query": "test", "facet": ""}],
            estimated_cost={"max_search_branches": 1},
            rationale="test",
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
            budget_snapshot=_make_budget_snapshot(),
        )
        assert result.valid is True

    def test_validate_proposal_rejected(self):
        result = self.service.validate_proposal(
            run_id=uuid4(),
            run_revision=5,
            coverage_revision=5,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            rationale="test",
            current_run_revision=3,  # 3 < 5 — stale
            current_coverage_revision=5,
            run_state="coverage_review",
            is_terminal=False,
        )
        assert result.valid is False

    def test_validate_proposal_scope_expansion_approved(self):
        expansion = ScopeExpansionRationale(
            expansion_type=ScopeExpansionType.NEW_ENTITIES,
            rationale="Need broader coverage",
            approved=True,
        )
        result = self.service.validate_proposal(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[{"query": "broader query", "facet": ""}],
            rationale="test",
            scope_expansion=expansion,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
        )
        assert result.valid is True
        assert result.scope_expansion is not None
        assert result.scope_expansion.approved is True

    def test_validate_proposal_scope_expansion_unjustified(self):
        expansion = ScopeExpansionRationale(
            expansion_type=ScopeExpansionType.NEW_ENTITIES,
            rationale="Need broader coverage",
            approved=False,
        )
        result = self.service.validate_proposal(
            run_id=uuid4(),
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[{"query": "broader query", "facet": ""}],
            rationale="test",
            scope_expansion=expansion,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
        )
        assert result.valid is False
        assert RejectionReason.SCOPE_EXPANSION_UNJUSTIFIED in result.rejection_reasons

    def test_authorize_scope_expansion_approved(self):
        run_id = uuid4()
        proposal = self.service.create_proposal(
            run_id=run_id,
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[{"query": "broader query", "facet": ""}],
            rationale="Need broader coverage",
        )
        expansion = ScopeExpansionRationale(
            expansion_type=ScopeExpansionType.NEW_ENTITIES,
            rationale="Need broader coverage",
            approved=True,
        )
        decision = self.service.authorize(
            run_id=run_id,
            proposal_id=proposal.proposal_id,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
            scope_expansion=expansion,
        )
        assert decision.outcome == "accepted"
        assert decision.scope_expansion is not None
        assert decision.scope_expansion.approved is True

    def test_authorize_scope_expansion_unjustified(self):
        run_id = uuid4()
        proposal = self.service.create_proposal(
            run_id=run_id,
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[{"query": "broader query", "facet": ""}],
            rationale="Need broader coverage",
        )
        expansion = ScopeExpansionRationale(
            expansion_type=ScopeExpansionType.NEW_ENTITIES,
            rationale="Need broader coverage",
            approved=False,
        )
        decision = self.service.authorize(
            run_id=run_id,
            proposal_id=proposal.proposal_id,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
            scope_expansion=expansion,
        )
        assert decision.outcome == "rejected"
        assert RejectionReason.SCOPE_EXPANSION_UNJUSTIFIED in decision.rejection_reasons

    # -- Idempotency --

    def test_proposal_idempotency(self):
        """Verify that duplicate proposal calls with the same idempotency key
        return the original proposal rather than creating a new one.

        NOTE: The in-memory repository does not simulate concurrent access.
        Real PostgreSQL idempotency relies on the unique constraint on
        (run_id, idempotency_key) and serializable transaction isolation.
        This test verifies the logical deduplication path only — concurrent
        race conditions are covered by the database-level unique constraint
        enforced in the migration (0013_strategy_revisions).
        """
        run_id = uuid4()
        idem_key = "test-idempotency-key"
        proposal1 = self.service.create_proposal(
            run_id=run_id,
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            rationale="test",
            idempotency_key=idem_key,
        )
        proposal2 = self.service.create_proposal(
            run_id=run_id,
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            rationale="test",
            idempotency_key=idem_key,
        )
        assert proposal1.proposal_id == proposal2.proposal_id

    def test_revision_order_is_monotonically_increasing(self):
        """Verify that each new proposal/decision gets a strictly higher
        revision_order than all previous rows for the same run.

        This test exercises the in-memory repository's revision_order
        computation.  In PostgreSQL the same ordering is achieved via the
        subquery ``COALESCE(MAX(revision_order), 0) + 1`` inside the
        ``record_proposal`` / ``record_decision`` INSERT, protected by
        the advisory lock from ``_lock_workflow_run``.
        """
        run_id = uuid4()
        # Create a proposal
        p = self.service.create_proposal(
            run_id=run_id,
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            rationale="first",
        )
        # Authorize it (creates a decision row)
        d = self.service.authorize(
            run_id=run_id,
            proposal_id=p.proposal_id,
            current_run_revision=1,
            current_coverage_revision=1,
            run_state="coverage_review",
            is_terminal=False,
        )
        # Create a second proposal
        p2 = self.service.create_proposal(
            run_id=run_id,
            run_revision=2,
            coverage_revision=2,
            decision_type="scrape",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            rationale="second",
        )
        # List proposals ordered by revision_order descending
        proposals = self.service.list_proposals(run_id)
        assert len(proposals) == 2
        # The second proposal (higher revision_order) should come first
        # when sorted descending
        assert proposals[0].proposal_id == p2.proposal_id
        assert proposals[1].proposal_id == p.proposal_id

        # List decisions
        decisions = self.service.list_decisions(run_id)
        assert len(decisions) == 1
        assert decisions[0].decision_id == d.decision_id

    # -- Proposal existence --

    def test_proposal_exists(self):
        run_id = uuid4()
        proposal = self.service.create_proposal(
            run_id=run_id,
            run_revision=1,
            coverage_revision=1,
            decision_type="search",
            target_coverage_item_ids=[uuid4()],
            proposed_queries=[],
            rationale="test",
        )
        assert self.repo.proposal_exists(run_id, proposal.proposal_id) is True
        assert self.repo.proposal_exists(run_id, uuid4()) is False


# ---------------------------------------------------------------------------
# Tests — Domain model invariants
# ---------------------------------------------------------------------------


class TestDomainModelInvariants:
    """Tests for domain model validation invariants."""

    def test_strategy_revision_decision_accepted_no_rejection_reasons(self):
        with pytest.raises(
            ValueError, match="accepted decisions must not include rejection reasons"
        ):
            StrategyRevisionDecision(
                decision_id=uuid4(),
                proposal_id=uuid4(),
                run_id=uuid4(),
                run_revision=1,
                coverage_revision=1,
                outcome="accepted",
                rejection_reasons=(RejectionReason.BUDGET_EXCEEDED,),
                policy_version="v1",
                scope_expansion=None,
                authorized_by="deterministic_policy",
                created_at=None,
            )

    def test_strategy_revision_decision_rejected_needs_reasons(self):
        with pytest.raises(
            ValueError, match="rejected decisions must include rejection reasons"
        ):
            StrategyRevisionDecision(
                decision_id=uuid4(),
                proposal_id=uuid4(),
                run_id=uuid4(),
                run_revision=1,
                coverage_revision=1,
                outcome="rejected",
                rejection_reasons=(),
                policy_version="v1",
                scope_expansion=None,
                authorized_by="deterministic_policy",
                created_at=None,
            )

    def test_strategy_revision_decision_scope_expansion_only_for_accepted(self):
        with pytest.raises(
            ValueError, match="scope_expansion is only recorded for accepted proposals"
        ):
            StrategyRevisionDecision(
                decision_id=uuid4(),
                proposal_id=uuid4(),
                run_id=uuid4(),
                run_revision=1,
                coverage_revision=1,
                outcome="rejected",
                rejection_reasons=(RejectionReason.BUDGET_EXCEEDED,),
                policy_version="v1",
                scope_expansion=ScopeExpansionRationale(
                    expansion_type=ScopeExpansionType.NEW_ENTITIES,
                    rationale="test",
                    approved=True,
                ),
                authorized_by="deterministic_policy",
                created_at=None,
            )

    def test_strategy_revision_decision_invalid_outcome(self):
        with pytest.raises(ValueError, match="outcome must be"):
            StrategyRevisionDecision(
                decision_id=uuid4(),
                proposal_id=uuid4(),
                run_id=uuid4(),
                run_revision=1,
                coverage_revision=1,
                outcome="pending",
                rejection_reasons=(),
                policy_version="v1",
                scope_expansion=None,
                authorized_by="deterministic_policy",
                created_at=None,
            )


# ---------------------------------------------------------------------------
# Tests — Rejection reason taxonomy
# ---------------------------------------------------------------------------


class TestRejectionReasonTaxonomy:
    """Verify all rejection reasons are defined."""

    def test_all_reasons_defined(self):
        expected = {
            "stale_coverage_revision",
            "stale_run_revision",
            "unknown_coverage_item",
            "unknown_run",
            "budget_exceeded",
            "scope_expanded",
            "scope_expansion_unjustified",
            "duplicate_action",
            "missing_rationale",
            "missing_target_items",
            "terminal_run_state",
            "unknown_decision_type",
        }
        actual = {r.value for r in RejectionReason}
        assert actual == expected

    def test_scope_expansion_types_defined(self):
        expected = {
            "new_entities",
            "new_jurisdictions",
            "new_time_windows",
            "new_source_classes",
            "new_archetype",
            "broadened_query_terms",
        }
        actual = {t.value for t in ScopeExpansionType}
        assert actual == expected


# ---------------------------------------------------------------------------
# Tests — Validation result helpers
# ---------------------------------------------------------------------------


class TestValidationResult:
    """Tests for ValidationResult factory methods."""

    def test_accepted_result(self):
        result = ValidationResult.accepted()
        assert result.valid is True
        assert len(result.rejection_reasons) == 0

    def test_rejected_result(self):
        result = ValidationResult.rejected(
            RejectionReason.STALE_RUN_REVISION,
            RejectionReason.TERMINAL_RUN_STATE,
        )
        assert result.valid is False
        assert len(result.rejection_reasons) == 2
        assert RejectionReason.STALE_RUN_REVISION in result.rejection_reasons
        assert RejectionReason.TERMINAL_RUN_STATE in result.rejection_reasons
