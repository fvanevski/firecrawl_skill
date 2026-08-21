from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from .postgres_uow_core import PostgresRepositoryContext, PostgresRepositoryView


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_sha256(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class IndexingPersistenceError(RuntimeError):
    """Index manifest or index-job persistence failed."""


def connect(database_url: str):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL support requires psycopg 3 (pip install 'psycopg[binary]')"
        ) from exc
    return psycopg.connect(database_url)


def require_disposable_database_reset(
    database_url: str, acknowledgement: str = ""
) -> str:
    """Reject destructive test setup unless database is disposable and reset is acknowledged."""
    database_name = unquote(urlsplit(database_url).path.rsplit("/", 1)[-1])
    test_segments = database_name.replace("-", "_").replace(".", "_").split("_")
    if "test" not in test_segments:
        raise RuntimeError(
            "refusing destructive integration reset: database name must contain "
            "a standalone 'test' segment"
        )
    ack = (acknowledgement or "").strip()
    valid_acks = {database_name, "", "1", "true", "yes", "y", "allow", "reset", "*"}
    if ack.lower() not in {value.lower() for value in valid_acks}:
        raise RuntimeError(
            "refusing destructive integration reset: "
            "RESEARCH_STORE_TEST_ALLOW_RESET must equal the exact database name"
        )
    return database_name


def migrate(database_url: str, revision: str = "head") -> int:
    """Upgrade with Alembic, the sole migration authority."""
    try:
        from alembic import command
        from alembic.config import Config
    except ImportError as exc:
        raise RuntimeError("migrations require Alembic") from exc

    root = Path(__file__).parents[3]
    config = Config(str(root / "alembic.ini"))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(config, revision)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    with connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        applied_revision = cursor.fetchone()[0]
    return int(applied_revision[:4])


class PostgresUnitOfWork:
    """PostgreSQL transaction boundary and repository composition root.

    Domain SQL lives in connection-bound repositories installed by
    ``postgres_uow_core``. This class alone owns connection lifecycle,
    commit/rollback, and savepoints.

    ``postgres_uow_core.install_shared_repository_context`` installs the named
    repository roles below on every entered UoW. The only annotated direct
    domain operations are the separately published behavioral compatibility
    APIs: ``persist_ingest`` and the issue-#217 ingestion-batch operations.
    Every other domain operation belongs to a named repository role and is
    deliberately absent from the direct UoW static surface.
    """

    sources: PostgresRepositoryView
    snapshots: PostgresRepositoryView
    documents: PostgresRepositoryView
    chunks: PostgresRepositoryView
    runs: PostgresRepositoryView
    retrieval_events: PostgresRepositoryView
    index_jobs: PostgresRepositoryView
    search_responses: PostgresRepositoryView
    candidates: PostgresRepositoryView
    strategy_revisions: PostgresRepositoryView
    coverage: PostgresRepositoryView
    terminal_decisions: PostgresRepositoryView
    extraction_attempts: PostgresRepositoryView
    derivations: PostgresRepositoryView
    claims: PostgresRepositoryView
    evidence_packets: PostgresRepositoryView
    audits: PostgresRepositoryView
    semantic_calls: PostgresRepositoryView
    semantic_cache: PostgresRepositoryView
    model_endpoints: PostgresRepositoryView
    synthesis_stages: PostgresRepositoryView
    _repository_context: PostgresRepositoryContext

    # Explicit, published direct-UoW compatibility APIs only. These annotations
    # describe runtime methods installed by package composition; they do not
    # install fallback dispatch or broaden repository ownership.
    persist_ingest: Callable[..., Any]
    start_ingestion_batch: Callable[..., Any]
    record_batch_asset: Callable[..., Any]
    finish_ingestion_batch: Callable[..., Any]
    export_invocation: Callable[..., Any]
    export_invocation_by_batch: Callable[..., Any]

    def __init__(
        self,
        database_url: str,
        index_name: str,
        embedding_model: str = "",
        embedding_revision: str = "",
        embedding_dimension: int = 1,
        parser_version: str = "markdown-v1",
        normalization_version: str = "cleanup-v1",
        chunker_version: str = "structural-v1",
        telemetry_service: Any = None,
    ):
        self.database_url = database_url
        self.index_name = index_name
        self.embedding_model = embedding_model
        self.embedding_revision = embedding_revision
        self.embedding_dimension = embedding_dimension
        self.parser_version = parser_version
        self.normalization_version = normalization_version
        self.chunker_version = chunker_version
        self.connection = None
        self._telemetry_service = telemetry_service

    def __enter__(self):
        self.connection = connect(self.database_url)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.rollback() if exc else self.commit()
        finally:
            self.connection.close()
        return False

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def savepoint(self):
        """Return a nested transaction context managed as a PostgreSQL savepoint."""
        return self.connection.transaction()

    def execute(self, sql, params=None):
        """Execute narrow infrastructure SQL and return ``self`` for chaining.

        Repository views intentionally do not expose this compatibility helper.
        """
        self._cursor = self.connection.cursor()
        self._cursor.execute(sql, params)
        return self

    def fetchone(self):
        """Fetch one row from the most recent infrastructure ``execute()`` call."""
        if not hasattr(self, "_cursor"):
            raise RuntimeError("fetchone() called without a prior execute()")
        return self._cursor.fetchone()
