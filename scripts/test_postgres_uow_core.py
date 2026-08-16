"""Regression coverage for the shared-connection PostgreSQL repository/UoW core."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.blob import ContentAddressedBlobStore
from research_store.postgres import PostgresUnitOfWork, connect, migrate
from research_store.postgres_uow_core import REPOSITORY_ROLES, PostgresRepositoryView

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
INTEGRATION = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


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


class TestPostgresRepositoryCore:
    def test_uow_binds_distinct_views_to_one_exact_connection(self, monkeypatch):
        fake_connection = _FakeConnection()
        monkeypatch.setattr(
            "research_store.postgres.connect", lambda _database_url: fake_connection
        )

        with PostgresUnitOfWork("postgresql://test.invalid/db", "test-index") as uow:
            repositories = [getattr(uow, role) for role in REPOSITORY_ROLES]

            assert all(
                isinstance(repo, PostgresRepositoryView) for repo in repositories
            )
            assert all(repo is not uow for repo in repositories)
            assert len({id(repo) for repo in repositories}) == len(REPOSITORY_ROLES)
            assert {repo.connection_identity for repo in repositories} == {
                id(fake_connection)
            }
            assert uow._repository_context.connection_identity == id(fake_connection)

        assert fake_connection.commits == 1
        assert fake_connection.closes == 1

    def test_repository_views_expose_domain_operations_not_uow_infrastructure(
        self, monkeypatch
    ):
        fake_connection = _FakeConnection()
        monkeypatch.setattr(
            "research_store.postgres.connect", lambda _database_url: fake_connection
        )

        with PostgresUnitOfWork("postgresql://test.invalid/db", "test-index") as uow:
            repository = uow.runs
            blocked = (
                "connection",
                "commit",
                "rollback",
                "savepoint",
                "close",
                "execute",
                "fetchone",
                "__enter__",
                "__exit__",
                "_lock_workflow_run",
                "_cursor",
                "_implementation",
                "_connection",
            )
            for name in blocked:
                assert not hasattr(repository, name), name
                assert name not in dir(repository), name

            # A normal public domain operation remains delegated through the
            # compatibility seam; only UoW/infrastructure capability is filtered.
            assert callable(repository.start_run)

            assert callable(uow.commit)
            assert callable(uow.rollback)
            assert callable(uow.execute)
            assert callable(uow.fetchone)
            assert callable(uow.savepoint)
            with uow.savepoint():
                pass

        assert fake_connection.transactions == 1

    def test_repository_cannot_reach_transaction_sql_via_generic_executor(
        self, monkeypatch
    ):
        fake_connection = _FakeConnection()
        monkeypatch.setattr(
            "research_store.postgres.connect", lambda _database_url: fake_connection
        )

        with PostgresUnitOfWork("postgresql://test.invalid/db", "test-index") as uow:
            repository = uow.runs
            for sql in ("COMMIT", "ROLLBACK", "SAVEPOINT repository_owned"):
                with pytest.raises(
                    AttributeError, match="does not expose UoW capability"
                ):
                    repository.execute(sql)

            # The failed capability lookups cannot have reached the connection.
            assert fake_connection.commits == 0
            assert fake_connection.rollbacks == 0
            assert fake_connection.transactions == 0

        # Only the containing UoW commits during normal context exit.
        assert fake_connection.commits == 1
        assert fake_connection.closes == 1


def _start_run(uow: PostgresUnitOfWork, suffix: str) -> UUID:
    return uow.runs.start_run(
        f"issue-255 {suffix}",
        {
            "external_run_id": f"issue-255-{suffix}-{uuid4()}",
            "execution_mode": "agent_led",
            "metadata": {"test": "issue-255"},
        },
    )


def _record_response(
    uow: PostgresUnitOfWork,
    run_id: UUID,
    blob_store: ContentAddressedBlobStore,
    suffix: str,
) -> dict:
    return uow.search_responses.record_search_response(
        run_id,
        f"issue-255 query {suffix}",
        "firecrawl",
        json.dumps({"success": True, "data": []}),
        f"issue-255-response-{suffix}-{uuid4()}",
        blob_store,
    )


@INTEGRATION
def test_cross_repository_writes_share_one_outer_rollback(tmp_path):
    migrate(TEST_DSN)
    blob_store = ContentAddressedBlobStore(tmp_path / "blobs")
    run_id = None
    response_id = None

    with (
        pytest.raises(RuntimeError, match="force outer rollback"),
        PostgresUnitOfWork(TEST_DSN, "test-index") as uow,
    ):
        assert uow.runs is not uow.search_responses
        assert uow.runs.connection_identity == uow.search_responses.connection_identity
        assert uow.runs.connection_identity == id(uow.connection)

        run_id = _start_run(uow, "outer-rollback")
        response = _record_response(uow, run_id, blob_store, "outer-rollback")
        response_id = response["id"]
        raise RuntimeError("force outer rollback")

    assert run_id is not None
    assert response_id is not None
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM research_runs WHERE id = %s", (run_id,))
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT count(*) FROM search_responses WHERE id = %s", (response_id,)
        )
        assert cursor.fetchone()[0] == 0


@INTEGRATION
def test_savepoint_rollback_stays_inside_containing_uow(tmp_path):
    migrate(TEST_DSN)
    blob_store = ContentAddressedBlobStore(tmp_path / "blobs")
    response_id = None

    with PostgresUnitOfWork(TEST_DSN, "test-index") as uow:
        run_id = _start_run(uow, "savepoint")
        try:
            with uow.savepoint():
                response = _record_response(uow, run_id, blob_store, "savepoint")
                response_id = response["id"]
                raise ValueError("rollback constituent work")
        except ValueError as exc:
            assert str(exc) == "rollback constituent work"

        assert not hasattr(uow.search_responses, "rollback")
        assert not hasattr(uow.search_responses, "execute")
        assert (
            uow.runs.connection_identity == uow.search_responses.connection_identity
        )
        assert uow.runs.connection_identity == id(uow.connection)

    assert response_id is not None
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM research_runs WHERE id = %s", (run_id,))
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "SELECT count(*) FROM search_responses WHERE id = %s", (response_id,)
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute("DELETE FROM research_runs WHERE id = %s", (run_id,))
