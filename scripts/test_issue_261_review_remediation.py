"""Regression coverage for the independent review remediation of PR #281 / #261."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")


class TestProductionSmartComposition:
    """The actual smart production builder must retain bounded/checkpoint semantics."""

    def test_provenance_builder_is_bounded_and_checkpoint_aware(self, monkeypatch):
        from research_store import container
        from research_store.bounded_orchestrator import (
            BoundedAcquisitionStage,
            BoundedExtractionStage,
        )
        from research_store.checkpoint_indexing_stage import CheckpointIndexingStage
        from research_store.checkpoint_orchestrator import (
            CheckpointResearchOrchestrator,
        )
        from research_store.orchestrator import OrchestratorConfig
        from research_store.search_provenance import (
            ProvenanceResumableResearchOrchestrator,
        )
        from research_store.terminal_decision import TerminalDecisionConfig

        run_service = MagicMock()
        run_service.uow_factory = MagicMock()
        run_service.checkpoint_indexing_enabled = True

        acquisition_service = MagicMock()
        strategy_service = MagicMock()
        corpus_service = MagicMock()
        extraction_service = MagicMock()
        evidence_service = MagicMock()

        monkeypatch.setattr(container, "build_run_service", lambda _config: run_service)
        monkeypatch.setattr(
            container, "build_acquisition_service", lambda _config: acquisition_service
        )
        monkeypatch.setattr(
            container, "build_strategy_service", lambda _config: strategy_service
        )
        monkeypatch.setattr(container, "build_service", lambda _config: corpus_service)
        monkeypatch.setattr(
            container, "build_extraction_service", lambda _config: extraction_service
        )
        monkeypatch.setattr(
            container, "build_evidence_service", lambda _config: evidence_service
        )

        config = MagicMock()
        config.require_database.return_value = None

        for builder in (
            CheckpointResearchOrchestrator,
            ProvenanceResumableResearchOrchestrator,
        ):
            orchestrator = builder.build(
                config,
                orchestrator_config=OrchestratorConfig(),
                terminal_config=TerminalDecisionConfig(),
            )

            assert isinstance(orchestrator, CheckpointResearchOrchestrator)
            assert isinstance(orchestrator._acquisition, BoundedAcquisitionStage)
            assert isinstance(orchestrator._extraction, BoundedExtractionStage)
            assert isinstance(orchestrator._indexing, CheckpointIndexingStage)
            assert (
                orchestrator._execute_stage.__func__
                is CheckpointResearchOrchestrator._execute_stage
            )
            assert (
                orchestrator._failed_result.__func__
                is CheckpointResearchOrchestrator._failed_result
            )

    def test_fsearch_smart_routes_through_resumable_composition_root(self):
        source = (SCRIPTS_DIR / "fsearch_smart").read_text(encoding="utf-8")
        assert "build_production_resumable_orchestrator" in source
        assert "ProvenanceResumableResearchOrchestrator.build" not in source


class TestResumeDependencyDirection:
    """Canonical resume code must not import semantic helpers from its facade."""

    def test_resume_use_case_has_no_smart_orchestrator_dependency(self):
        source = (
            SCRIPTS_DIR / "research_store" / "orchestration" / "resume.py"
        ).read_text(encoding="utf-8")
        assert "smart_orchestrator" not in source
        assert "resume_support" in source

    def test_resume_support_has_no_infrastructure_or_facade_dependency(self):
        source = (
            SCRIPTS_DIR / "research_store" / "orchestration" / "resume_support.py"
        ).read_text(encoding="utf-8")
        assert "smart_orchestrator" not in source
        assert ".cursor(" not in source
        assert "psycopg" not in source


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_resume_strategy_order_packet_revision_and_branch_cap():
    """Persisted strategy order must survive resume and determine the capped subset."""
    from budget_policy import DEFAULT_POLICY, conservative_research_spec
    from research_domain import load_model, serialize_model
    from research_store import postgres as pg
    from research_store.bounded_orchestrator import BoundedAcquisitionStage
    from research_store.postgres import migrate
    from research_store.resume_state_repository import PostgresResumeStateReader

    migrate(TEST_DSN)

    run_id = uuid4()
    first_proposal = uuid4()
    rejected_proposal = uuid4()
    second_proposal = uuid4()

    with pg.PostgresUnitOfWork(TEST_DSN, "issue261-review") as uow:
        with uow.connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_runs (
                    id, objective, query_plan, skill_version, llm_model,
                    state, execution_mode
                ) VALUES (%s, 'issue-261-review', '{}', '1.0', 'm',
                          'acquiring', 'autonomous_local')""",
                (run_id,),
            )

        spec_id = uow.runs.record_research_spec(
            run_id,
            spec_revision=1,
            schema_name="research_spec",
            schema_version=1,
            payload={"schema_version": "research-spec-test"},
            idempotency_key=f"review:spec:{run_id}",
        )

        def proposal(proposal_id, query_text, key):
            return uow.strategy_revisions.record_proposal(
                run_id,
                proposal_id,
                0,
                1,
                "search",
                [],
                [{"query": query_text, "facet": "adaptive"}],
                [],
                [],
                "review regression",
                {},
                "review regression",
                1.0,
                key,
            )

        def decision(proposal_id, outcome, key):
            return uow.strategy_revisions.record_decision(
                run_id,
                uuid4(),
                proposal_id,
                0,
                1,
                outcome,
                [] if outcome == "accepted" else ["review-rejected"],
                "budget-policy-v1",
                None,
                None,
                None,
                "review-test",
                key,
            )

        proposal(first_proposal, "adaptive-oldest", f"review:p1:{run_id}")
        decision(first_proposal, "accepted", f"review:d1:{run_id}")
        proposal(rejected_proposal, "adaptive-rejected", f"review:p2:{run_id}")
        decision(rejected_proposal, "rejected", f"review:d2:{run_id}")
        proposal(second_proposal, "adaptive-newest", f"review:p3:{run_id}")
        decision(second_proposal, "accepted", f"review:d3:{run_id}")

        uow.evidence_packets.persist_evidence_packet(
            run_id,
            spec_id,
            coverage_revision=1,
            packet_revision=7,
            payload={"schema_version": "review-packet"},
        )
        uow.commit()

    reader = PostgresResumeStateReader(
        lambda: pg.PostgresUnitOfWork(TEST_DSN, "issue261-review")
    )
    authorized = reader.authorized_queries(run_id)
    assert [row["proposal_id"] for row in authorized] == [
        str(first_proposal),
        str(second_proposal),
    ]
    assert [row["proposed_queries"][0]["query"] for row in authorized] == [
        "adaptive-oldest",
        "adaptive-newest",
    ]
    assert reader.packet_revision(run_id) == 7

    spec = serialize_model(conservative_research_spec("resume order", "general"))
    budget = DEFAULT_POLICY.evaluate(
        load_model(spec),
        spec_revision=1,
        run_revision=0,
    )
    cap = budget.effective_caps.max_search_branches
    assert cap >= 2

    original_query = "original-already-executed"
    executed = {original_query}
    executed.update(f"already-{index}" for index in range(max(0, cap - 2)))
    assert len(executed) == cap - 1

    calls: list[str] = []

    class Acquisition:
        def execute_search(self, _run_id, query_text, **_kwargs):
            calls.append(query_text)
            return SimpleNamespace(
                search_response_id=uuid4(),
                candidate_count=0,
                candidates=[],
                search_response={},
            )

    run_service = MagicMock()
    stage = BoundedAcquisitionStage(
        run_service=run_service,
        acquisition_service=Acquisition(),
        coverage_service=MagicMock(),
        strategy_service=MagicMock(),
        config=MagicMock(),
    )
    context = {
        "spec": spec,
        "search_plan": {"queries": [{"query": original_query}]},
        "authorized_queries": authorized,
        "executed_query_texts": executed,
    }
    result = stage.execute(
        run_id,
        0,
        None,
        "acquiring",
        context,
    )

    assert result.error is None
    assert calls == ["adaptive-oldest"]
    run_service.transition.assert_called_once()


def test_resume_reader_uses_single_canonical_strategy_projection():
    """The adapter must not reconstruct accepted decisions with an N+1 read loop."""
    from research_store.resume_state_repository import PostgresResumeStateReader

    expected = [
        {
            "proposal_id": str(uuid4()),
            "decision_type": "search",
            "proposed_queries": [{"query": "q"}],
        }
    ]
    uow = MagicMock()
    uow.__enter__.return_value = uow
    uow.__exit__.return_value = False
    uow.strategy_revisions.list_accepted_search_proposals.return_value = expected

    reader = PostgresResumeStateReader(lambda: uow)
    assert reader.authorized_queries(uuid4()) == expected
    uow.strategy_revisions.list_accepted_search_proposals.assert_called_once()
    uow.strategy_revisions.list_proposals.assert_not_called()
    uow.strategy_revisions.list_decision_ids_for_proposal.assert_not_called()
    uow.strategy_revisions.get_decision.assert_not_called()
