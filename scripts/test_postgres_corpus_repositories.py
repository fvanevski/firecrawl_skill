"""Focused regressions for issue #256 PostgreSQL corpus repositories."""

from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.postgres import PostgresUnitOfWork
from research_store.postgres_corpus import PostgresCorpusRepository
from research_store.postgres_derivations import PostgresDerivationRepository


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


def test_corpus_and_derivation_methods_bind_to_canonical_repositories(monkeypatch):
    connection = _FakeConnection()
    monkeypatch.setattr(
        "research_store.postgres.connect", lambda _database_url: connection
    )

    with PostgresUnitOfWork("postgresql://test.invalid/db", "test-index") as uow:
        assert uow.snapshots.connection_identity == id(connection)
        assert uow.documents.connection_identity == id(connection)
        assert uow.chunks.connection_identity == id(connection)
        assert uow.derivations.connection_identity == id(connection)

        assert isinstance(uow.snapshots.persist_ingest.__self__, PostgresCorpusRepository)
        assert isinstance(uow.persist_ingest.__self__, PostgresCorpusRepository)
        assert isinstance(
            uow.start_ingestion_batch.__self__, PostgresCorpusRepository
        )
        assert isinstance(uow.derivations.create.__self__, PostgresDerivationRepository)
        assert isinstance(uow.create.__self__, PostgresDerivationRepository)

        canonical_repositories = (
            uow.persist_ingest.__self__,
            uow.start_ingestion_batch.__self__,
            uow.derivations.create.__self__,
        )
        for repository in canonical_repositories:
            assert not hasattr(repository, "connection")
            assert not hasattr(repository, "commit")
            assert not hasattr(repository, "rollback")
            assert not hasattr(repository, "savepoint")

        for repository in (
            uow.sources,
            uow.snapshots,
            uow.documents,
            uow.chunks,
            uow.derivations,
        ):
            assert not hasattr(repository, "connection")
            assert not hasattr(repository, "commit")
            assert not hasattr(repository, "rollback")
            assert not hasattr(repository, "savepoint")

        with uow.savepoint():
            pass

    assert connection.transactions == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closes == 1


def test_issue_217_batch_functions_are_repository_bound(monkeypatch):
    connection = _FakeConnection()
    monkeypatch.setattr(
        "research_store.postgres.connect", lambda _database_url: connection
    )

    with PostgresUnitOfWork("postgresql://test.invalid/db", "test-index") as uow:
        for name in (
            "start_ingestion_batch",
            "record_batch_asset",
            "finish_ingestion_batch",
            "export_invocation",
            "export_invocation_by_batch",
        ):
            operation = getattr(uow, name)
            assert isinstance(operation.__self__, PostgresCorpusRepository)
            assert not hasattr(uow.snapshots, "commit")
            assert not hasattr(uow.snapshots, "rollback")

        # #217 patches the UoW class after #255/#256 installation; the
        # instance compatibility facade must still win.
        assert uow.start_ingestion_batch.__self__ is uow.persist_ingest.__self__
