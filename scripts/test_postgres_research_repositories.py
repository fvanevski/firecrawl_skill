"""Focused regressions for issue #257 PostgreSQL research repositories."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.postgres import PostgresUnitOfWork, migrate
from research_store.postgres_coverage import PostgresCoverageRepository
from research_store.postgres_research import PostgresResearchRepository
from research_store.postgres_strategy import PostgresStrategyRevisionRepository
from research_store.postgres_terminal import PostgresTerminalDecisionRepository
from research_store.run_service import ResearchRunService, RunStateError

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")


class _FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeConnection:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.transactions = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closes += 1

    def transaction(self) -> _FakeTransaction:
        self.transactions += 1
        return _FakeTransaction()


def test_research_workflow_roles_bind_to_canonical_repositories(monkeypatch):
    connection = _FakeConnection()
    monkeypatch.setattr(
        "research_store.postgres.connect", lambda _database_url: connection
    )

    with PostgresUnitOfWork("postgresql://test.invalid/db", "test-index") as uow:
        for role in (
            uow.runs,
            uow.coverage,
            uow.strategy_revisions,
            uow.terminal_decisions,
        ):
            assert role.connection_identity == id(connection)
            assert not hasattr(role, "connection")
            assert not hasattr(role, "commit")
            assert not hasattr(role, "rollback")
            assert not hasattr(role, "savepoint")

        assert isinstance(uow.runs.start_run.__self__, PostgresResearchRepository)
        assert isinstance(uow.start_run.__self__, PostgresResearchRepository)
        assert isinstance(uow.coverage.apply_event.__self__, PostgresCoverageRepository)
        assert isinstance(uow.apply_event.__self__, PostgresCoverageRepository)
        assert isinstance(
            uow.strategy_revisions.record_proposal.__self__,
            PostgresStrategyRevisionRepository,
        )
        assert isinstance(
            uow.record_proposal.__self__, PostgresStrategyRevisionRepository
        )
        assert isinstance(
            uow.terminal_decisions.record_terminal_decision.__self__,
            PostgresTerminalDecisionRepository,
        )
        assert isinstance(
            uow.record_terminal_decision.__self__,
            PostgresTerminalDecisionRepository,
        )

        canonical_repositories = (
            uow.runs.start_run.__self__,
            uow.coverage.apply_event.__self__,
            uow.strategy_revisions.record_proposal.__self__,
            uow.terminal_decisions.record_terminal_decision.__self__,
        )
        for repository in canonical_repositories:
            assert not hasattr(repository, "connection")
            assert not hasattr(repository, "commit")
            assert not hasattr(repository, "rollback")
            assert not hasattr(repository, "savepoint")

        assert callable(uow.runs._lock_workflow_run)
        assert callable(uow.runs._bump_lifecycle_revision)

        # Successor scope is intentionally not absorbed by #257. Acquisition
        # (#258) and evidence/semantic state (#259) continue through the
        # temporary legacy fallback until their own extraction issues land.
        assert uow.runs.record_search_plan.__self__ is uow
        assert uow.runs.record_semantic_call.__self__ is uow

        with uow.savepoint():
            pass

    assert connection.transactions == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closes == 1


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_research_repositories_share_outer_uow_rollback():
    """Run, coverage, and strategy writes roll back as one UoW transaction."""
    migrate(TEST_DSN)
    suffix = uuid4().hex
    external_id = f"issue-257-rollback-{suffix}"
    proposal_id = uuid4()
    run_id = None

    with (
        pytest.raises(RuntimeError, match="force research rollback"),
        PostgresUnitOfWork(TEST_DSN, "issue-257-test-index") as uow,
    ):
        for role in (
            uow.runs,
            uow.coverage,
            uow.strategy_revisions,
            uow.terminal_decisions,
        ):
            assert role.connection_identity == id(uow.connection)

        run_id = uow.runs.start_run(
            "issue 257 shared rollback",
            {
                "external_run_id": external_id,
                "execution_mode": "agent_led",
                "metadata": {"issue": 257},
            },
        )
        item_ids = uow.coverage.create_items(
            run_id,
            [
                {
                    "item_type": "question",
                    "subject_id": f"question-{suffix}",
                    "text": "rollback coverage item",
                }
            ],
            f"coverage:{suffix}",
        )
        assert len(item_ids) == 1
        proposal = uow.strategy_revisions.record_proposal(
            run_id,
            proposal_id,
            0,
            1,
            "search",
            [str(item_ids[0])],
            ["postgres transaction evidence"],
            [],
            [],
            "exercise one shared transaction",
            {"queries": 1},
            "issue 257 rollback regression",
            0.9,
            f"strategy:{suffix}",
            actor_type="test",
        )
        assert proposal["proposal_id"] == str(proposal_id)
        raise RuntimeError("force research rollback")

    assert run_id is not None
    with PostgresUnitOfWork(TEST_DSN, "issue-257-test-index") as uow:
        with pytest.raises(KeyError):
            uow.runs.get_run_status(external_id=external_id)
        assert uow.coverage.count_coverage_items(run_id) == 0
        assert uow.strategy_revisions.list_proposals(run_id) == []


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_research_repositories_obey_uow_savepoint_rollback():
    """A nested UoW savepoint rolls back repository writes but not the run."""
    migrate(TEST_DSN)
    suffix = uuid4().hex
    external_id = f"issue-257-savepoint-{suffix}"
    proposal_id = uuid4()

    with PostgresUnitOfWork(TEST_DSN, "issue-257-test-index") as uow:
        run_id = uow.runs.start_run(
            "issue 257 savepoint rollback",
            {
                "external_run_id": external_id,
                "execution_mode": "agent_led",
                "metadata": {"issue": 257},
            },
        )
        try:
            with uow.savepoint():
                item_ids = uow.coverage.create_items(
                    run_id,
                    [
                        {
                            "item_type": "question",
                            "subject_id": f"question-{suffix}",
                            "text": "savepoint coverage item",
                        }
                    ],
                    f"coverage-savepoint:{suffix}",
                )
                uow.strategy_revisions.record_proposal(
                    run_id,
                    proposal_id,
                    0,
                    1,
                    "search",
                    [str(item_ids[0])],
                    ["savepoint evidence"],
                    [],
                    [],
                    "exercise nested transaction",
                    {"queries": 1},
                    "issue 257 savepoint regression",
                    0.9,
                    f"strategy-savepoint:{suffix}",
                    actor_type="test",
                )
                raise RuntimeError("force savepoint rollback")
        except RuntimeError as exc:
            assert str(exc) == "force savepoint rollback"

        status = uow.runs.get_run_status(run_id=run_id)
        assert status["state"] == "created"
        assert uow.coverage.count_coverage_items(run_id) == 0
        assert uow.strategy_revisions.list_proposals(run_id) == []

    with PostgresUnitOfWork(TEST_DSN, "issue-257-test-index") as uow:
        assert uow.runs.get_run_status(run_id=run_id)["state"] == "created"
        assert uow.coverage.count_coverage_items(run_id) == 0
        assert uow.strategy_revisions.list_proposals(run_id) == []


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_terminal_decision_and_run_transition_are_one_transaction():
    """Failed terminal transitions roll back provenance; valid ones commit both."""
    migrate(TEST_DSN)
    suffix = uuid4().hex
    external_id = f"issue-257-terminal-{suffix}"

    with PostgresUnitOfWork(TEST_DSN, "issue-257-test-index") as uow:
        run_id = uow.runs.start_run(
            "issue 257 terminal provenance",
            {
                "external_run_id": external_id,
                "execution_mode": "agent_led",
                "metadata": {"issue": 257},
            },
        )
        transition = uow.runs.apply_run_transition(
            run_id,
            "planning",
            0,
            f"planning:{suffix}",
            "test",
            "run-state-v1",
            permitted_prior_states=frozenset({"created"}),
            event_type="run.transitioned.planning",
        )
        assert transition["lifecycle_revision"] == 1
        item_ids = uow.coverage.create_items(
            run_id,
            [
                {
                    "item_type": "question",
                    "subject_id": f"terminal-question-{suffix}",
                    "text": "terminal provenance coverage item",
                }
            ],
            f"terminal-coverage:{suffix}",
        )
        assert len(item_ids) == 1
        assert uow.coverage.get_current_revision(run_id) == 1

    service = ResearchRunService(
        lambda: PostgresUnitOfWork(TEST_DSN, "issue-257-test-index")
    )
    rolled_back_key = f"terminal-invalid:{suffix}"
    rolled_back_decision = uuid4()

    # planning -> partial is invalid. The terminal repository insert occurs
    # first, so absence afterward proves the shared outer UoW rolled it back.
    with pytest.raises(RunStateError):
        service.commit_terminal_decision(
            run_id,
            decision_id=rolled_back_decision,
            run_revision=1,
            coverage_revision=1,
            outcome="partial",
            no_progress_signals=("no_new_sources",),
            unresolved_gap="invalid transition regression",
            policy_version="terminal-decision-v1",
            idempotency_key=rolled_back_key,
            created_at=datetime.now(timezone.utc),
            next_state="partial",
            expected_revision=1,
            actor_type="test",
            reason="must roll back provenance when transition is rejected",
        )

    with PostgresUnitOfWork(TEST_DSN, "issue-257-test-index") as uow:
        status = uow.runs.get_run_status(run_id=run_id)
        assert status["state"] == "planning"
        assert status["lifecycle_revision"] == 1
        with uow.connection.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM terminal_decisions
                WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, rolled_back_key),
            )
            assert cur.fetchone()[0] == 0

    committed_key = f"terminal-valid:{suffix}"
    committed_decision = uuid4()
    result = service.commit_terminal_decision(
        run_id,
        decision_id=committed_decision,
        run_revision=1,
        coverage_revision=1,
        outcome="failed",
        no_progress_signals=("no_new_sources",),
        unresolved_gap="terminal provenance regression",
        policy_version="terminal-decision-v1",
        idempotency_key=committed_key,
        created_at=datetime.now(timezone.utc),
        next_state="failed",
        expected_revision=1,
        actor_type="test",
        reason="valid terminal failure",
    )
    assert result["next_state"] == "failed"
    assert result["lifecycle_revision"] == 2

    with PostgresUnitOfWork(TEST_DSN, "issue-257-test-index") as uow:
        status = uow.runs.get_run_status(run_id=run_id)
        assert status["state"] == "failed"
        assert status["lifecycle_revision"] == 2
        with uow.connection.cursor() as cur:
            cur.execute(
                """SELECT decision_id, run_revision, coverage_revision, outcome
                FROM terminal_decisions
                WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, committed_key),
            )
            row = cur.fetchone()
        assert row is not None
        assert str(row[0]) == str(committed_decision)
        assert row[1:] == (1, 1, "failed")


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_concurrent_coverage_events_serialize_revision_allocation():
    """Two PostgreSQL writers allocate one monotonic coverage revision each."""
    from concurrent.futures import ThreadPoolExecutor

    migrate(TEST_DSN)
    suffix = uuid4().hex

    with PostgresUnitOfWork(TEST_DSN, "issue-257-test-index") as uow:
        run_id = uow.runs.start_run(
            "issue 257 concurrent coverage revisions",
            {
                "external_run_id": f"issue-257-coverage-concurrency-{suffix}",
                "execution_mode": "agent_led",
                "metadata": {"issue": 257},
            },
        )
        item_id = uow.coverage.create_items(
            run_id,
            [
                {
                    "item_type": "question",
                    "subject_id": f"coverage-concurrency-{suffix}",
                    "text": "serialize coverage revision allocation",
                }
            ],
            f"coverage-concurrency:create:{suffix}",
        )[0]
        assert uow.coverage.get_current_revision(run_id) == 1

    commands = tuple(
        (
            f"coverage-concurrency:event:{suffix}:{attempt}",
            str(uuid4()),
        )
        for attempt in range(2)
    )

    def apply_once(command):
        idempotency_key, candidate_id = command
        with PostgresUnitOfWork(TEST_DSN, "issue-257-test-index") as uow:
            return uow.coverage.apply_event(
                run_id,
                "candidate_identified",
                item_id=item_id,
                payload={"candidate_id": candidate_id},
                idempotency_key=idempotency_key,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(apply_once, commands))

    assert sorted(result["coverage_revision"] for result in results) == [2, 3]
    assert sorted(result["prior_coverage_revision"] for result in results) == [1, 2]
    assert len({result["id"] for result in results}) == 2

    first = results[0]
    first_key, first_candidate_id = commands[0]
    with PostgresUnitOfWork(TEST_DSN, "issue-257-test-index") as uow:
        replay = uow.coverage.apply_event(
            run_id,
            "candidate_identified",
            item_id=item_id,
            payload={"candidate_id": first_candidate_id},
            idempotency_key=first_key,
        )
        assert replay["id"] == first["id"]
        assert replay["coverage_revision"] == first["coverage_revision"]
        assert uow.coverage.get_current_revision(run_id) == 3

        with uow.connection.cursor() as cur:
            cur.execute(
                """SELECT coverage_revision, prior_coverage_revision
                FROM coverage_events
                WHERE run_id=%s AND idempotency_key=ANY(%s)
                ORDER BY coverage_revision""",
                (run_id, [command[0] for command in commands]),
            )
            assert cur.fetchall() == [(2, 1), (3, 2)]


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_concurrent_strategy_writers_serialize_shared_revision_order():
    """Proposal and decision writers share one serialized PostgreSQL order."""
    from concurrent.futures import ThreadPoolExecutor

    migrate(TEST_DSN)
    suffix = uuid4().hex

    with PostgresUnitOfWork(TEST_DSN, "issue-257-test-index") as uow:
        run_id = uow.runs.start_run(
            "issue 257 concurrent strategy revisions",
            {
                "external_run_id": f"issue-257-strategy-concurrency-{suffix}",
                "execution_mode": "agent_led",
                "metadata": {"issue": 257},
            },
        )
        transition = uow.runs.apply_run_transition(
            run_id,
            "planning",
            0,
            f"strategy-concurrency:planning:{suffix}",
            "test",
            "run-state-v1",
            permitted_prior_states=frozenset({"created"}),
            event_type="run.transitioned.planning",
        )
        assert transition["lifecycle_revision"] == 1
        item_id = uow.coverage.create_items(
            run_id,
            [
                {
                    "item_type": "question",
                    "subject_id": f"strategy-concurrency-{suffix}",
                    "text": "serialize strategy revision order",
                }
            ],
            f"strategy-concurrency:coverage:{suffix}",
        )[0]
        seed_proposal_id = uuid4()
        seed = uow.strategy_revisions.record_proposal(
            run_id,
            seed_proposal_id,
            1,
            1,
            "search",
            [str(item_id)],
            ["seed strategy concurrency"],
            [],
            [],
            "seed the shared strategy order",
            {"queries": 1},
            "issue 257 strategy concurrency seed",
            0.9,
            f"strategy-concurrency:seed:{suffix}",
            actor_type="test",
        )
        assert seed["revision_order"] == 1

    concurrent_proposal_id = uuid4()
    concurrent_decision_id = uuid4()
    proposal_key = f"strategy-concurrency:proposal:{suffix}"
    decision_key = f"strategy-concurrency:decision:{suffix}"

    def record_proposal():
        with PostgresUnitOfWork(TEST_DSN, "issue-257-test-index") as uow:
            return uow.strategy_revisions.record_proposal(
                run_id,
                concurrent_proposal_id,
                1,
                1,
                "search",
                [str(item_id)],
                ["concurrent proposal"],
                [],
                [],
                "exercise concurrent proposal ordering",
                {"queries": 1},
                "issue 257 concurrent proposal",
                0.8,
                proposal_key,
                actor_type="test",
            )

    def record_decision():
        with PostgresUnitOfWork(TEST_DSN, "issue-257-test-index") as uow:
            return uow.strategy_revisions.record_decision(
                run_id,
                concurrent_decision_id,
                seed_proposal_id,
                1,
                1,
                "accepted",
                [],
                "strategy-policy-v1",
                None,
                None,
                None,
                "deterministic-policy",
                decision_key,
                actor_type="test",
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        proposal_future = executor.submit(record_proposal)
        decision_future = executor.submit(record_decision)
        proposal = proposal_future.result()
        decision = decision_future.result()

    assert sorted((proposal["revision_order"], decision["revision_order"])) == [2, 3]
    assert proposal["proposal_id"] == str(concurrent_proposal_id)
    assert decision["decision_id"] == str(concurrent_decision_id)

    replayed_proposal = record_proposal()
    replayed_decision = record_decision()
    assert replayed_proposal["id"] == proposal["id"]
    assert replayed_proposal["revision_order"] == proposal["revision_order"]
    assert replayed_decision["id"] == decision["id"]
    assert replayed_decision["revision_order"] == decision["revision_order"]

    with PostgresUnitOfWork(TEST_DSN, "issue-257-test-index") as uow:
        with uow.connection.cursor() as cur:
            cur.execute(
                """SELECT row_type, revision_order, idempotency_key
                FROM strategy_revisions
                WHERE run_id=%s
                  AND idempotency_key=ANY(%s)
                ORDER BY revision_order""",
                (
                    run_id,
                    [
                        f"strategy-concurrency:seed:{suffix}",
                        proposal_key,
                        decision_key,
                    ],
                ),
            )
            rows = cur.fetchall()

    assert [row[1] for row in rows] == [1, 2, 3]
    assert {row[0] for row in rows[1:]} == {"proposal", "decision"}
