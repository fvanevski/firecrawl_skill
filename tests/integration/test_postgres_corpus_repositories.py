"""Focused regressions for issue #256 PostgreSQL corpus repositories."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from firecrawl_skill.research_store.postgres import PostgresUnitOfWork, migrate
from firecrawl_skill.research_store.postgres_corpus import PostgresCorpusRepository
from firecrawl_skill.research_store.postgres_derivations import (
    PostgresDerivationRepository,
)

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""


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
        "firecrawl_skill.research_store.postgres.connect",
        lambda _database_url: connection,
    )

    with PostgresUnitOfWork("postgresql://test.invalid/db", "test-index") as uow:
        assert uow.sources.connection_identity == id(connection)
        assert uow.snapshots.connection_identity == id(connection)
        assert uow.documents.connection_identity == id(connection)
        assert uow.chunks.connection_identity == id(connection)
        assert uow.derivations.connection_identity == id(connection)

        assert isinstance(uow.sources.upsert_source.__self__, PostgresCorpusRepository)
        assert isinstance(
            uow.snapshots.persist_ingest.__self__, PostgresCorpusRepository
        )
        assert isinstance(
            cast(Any, uow.persist_ingest).__self__, PostgresCorpusRepository
        )
        assert isinstance(
            cast(Any, uow.start_ingestion_batch).__self__, PostgresCorpusRepository
        )
        assert isinstance(uow.derivations.create.__self__, PostgresDerivationRepository)
        assert isinstance(cast(Any, uow.create).__self__, PostgresDerivationRepository)

        canonical_repositories = (
            uow.sources.upsert_source.__self__,
            cast(Any, uow.persist_ingest).__self__,
            cast(Any, uow.start_ingestion_batch).__self__,
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
        "firecrawl_skill.research_store.postgres.connect",
        lambda _database_url: connection,
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
        assert (
            cast(Any, uow.start_ingestion_batch).__self__
            is cast(Any, uow.persist_ingest).__self__
        )


@pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
def test_derivation_repository_uses_uow_transaction_and_rolls_back():
    """Exercise canonical derivation writes on a real UoW-owned connection."""
    migrate(TEST_DSN)
    suffix = uuid4().hex
    source_url = f"https://issue-256-{suffix}.example/"
    document_sha = ("a" * 32) + suffix
    snapshot_sha = ("b" * 32) + suffix
    active_configuration = ("c" * 32) + suffix
    rolled_back_configuration = ("d" * 32) + suffix

    with PostgresUnitOfWork(TEST_DSN, "issue-256-test-index") as uow:
        assert uow.derivations.connection_identity == id(uow.connection)
        assert uow.connection is not None
        with uow.connection.cursor() as cur:
            cur.execute(
                "INSERT INTO sources(canonical_url) VALUES (%s) RETURNING id",
                (source_url,),
            )
            source_id = cur.fetchone()
            assert source_id is not None
            source_id = source_id[0]
            cur.execute(
                """INSERT INTO asset_snapshots(
                    source_id, requested_url, retrieved_at, content_sha256
                ) VALUES (%s, %s, now(), %s) RETURNING id""",
                (source_id, source_url, snapshot_sha),
            )
            snapshot_id = cur.fetchone()
            assert snapshot_id is not None
            snapshot_id = snapshot_id[0]
            cur.execute(
                """INSERT INTO documents(
                    snapshot_id, normalized_text, parser_name, parser_version,
                    normalization_version, document_sha256
                ) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (
                    snapshot_id,
                    "issue 256 derivation transaction",
                    "markdown",
                    "markdown-v1",
                    "cleanup-v1",
                    document_sha,
                ),
            )
            document_id = cur.fetchone()
            assert document_id is not None
            document_id = document_id[0]

        created = uow.derivations.create(
            document_id=document_id,
            snapshot_id=snapshot_id,
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            chunker_name="structural",
            chunker_version="structural-v1",
            tokenizer_name="cl100k_base",
            configuration_sha256=active_configuration,
        )
        assert created.status == "pending"
        activated = uow.derivations.activate(created.id)
        assert activated.status == "active"

    with PostgresUnitOfWork(TEST_DSN, "issue-256-test-index") as uow:
        assert uow.derivations.connection_identity == id(uow.connection)
        persisted = uow.derivations.find_by_configuration(
            document_id, active_configuration
        )
        assert persisted is not None
        assert persisted["status"] == "active"

    with (
        pytest.raises(RuntimeError, match="force derivation rollback"),
        PostgresUnitOfWork(TEST_DSN, "issue-256-test-index") as uow,
    ):
        pending = uow.derivations.create(
            document_id=document_id,
            snapshot_id=snapshot_id,
            parser_version="markdown-v2",
            normalization_version="cleanup-v1",
            chunker_name="structural",
            chunker_version="structural-v1",
            tokenizer_name="cl100k_base",
            configuration_sha256=rolled_back_configuration,
        )
        assert pending.status == "pending"
        raise RuntimeError("force derivation rollback")

    with PostgresUnitOfWork(TEST_DSN, "issue-256-test-index") as uow:
        assert (
            uow.derivations.find_by_configuration(
                document_id, rolled_back_configuration
            )
            is None
        )
