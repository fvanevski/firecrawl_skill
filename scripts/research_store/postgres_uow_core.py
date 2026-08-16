"""Shared-connection PostgreSQL repository/UoW seam.

Issue #255 established explicit repository views and the one-connection
transaction boundary. Issue #256 replaces corpus/asset/derivation persistence
with canonical connection-bound repositories while retaining temporary
compatibility delegation for other Phase-3 domains.

The UoW remains the sole owner of connection lifecycle, commit, rollback, and
savepoints. Repository views do not expose transaction control, raw connection
access, or the generic SQL executor.
"""

from __future__ import annotations

from functools import wraps
from typing import Any

from .postgres_corpus import PostgresCorpusRepository
from .postgres_derivations import PostgresDerivationRepository

REPOSITORY_ROLES: tuple[str, ...] = (
    "sources",
    "snapshots",
    "documents",
    "chunks",
    "runs",
    "retrieval_events",
    "index_jobs",
    "search_responses",
    "candidates",
    "strategy_revisions",
    "coverage",
    "terminal_decisions",
    "extraction_attempts",
    "derivations",
    "semantic_cache",
    "model_endpoints",
    "synthesis_stages",
)

_NON_REPOSITORY_CAPABILITIES = frozenset(
    {
        "connection",
        "commit",
        "rollback",
        "savepoint",
        "close",
        "execute",
        "fetchone",
        "__enter__",
        "__exit__",
    }
)

_ROLE_PRIVATE_OPERATIONS: dict[str, frozenset[str]] = {
    "runs": frozenset({"_bump_lifecycle_revision", "_lock_workflow_run"}),
}

_CORPUS_ROLES = frozenset({"sources", "snapshots", "documents", "chunks"})
_CORPUS_COMPATIBILITY_OPERATIONS = (
    "persist_ingest",
    "ensure_index_definition",
    "link_run_asset",
    "start_ingestion_batch",
    "record_batch_asset",
    "finish_ingestion_batch",
    "export_invocation",
    "export_invocation_by_batch",
)
_DERIVATION_COMPATIBILITY_OPERATIONS = (
    "list_all_targets",
    "get_document_for_snapshot",
    "get_snapshots_for_document",
    "get_snapshot_info",
    "find_by_configuration",
    "activate",
    "count_chunks_for_derivation",
    "count_blocks_for_derivation",
    "list",
    "get",
    "create",
)
_INSTALL_MARKER = "_shared_repository_context_installed"


def _is_delegated_operation(role: str, name: str) -> bool:
    if name.startswith("_"):
        return name in _ROLE_PRIVATE_OPERATIONS.get(role, frozenset())
    return name not in _NON_REPOSITORY_CAPABILITIES


class PostgresRepositoryView:
    """Capability-filtered repository view with optional canonical implementation."""

    __slots__ = (
        "__canonical_implementation",
        "__connection_identity",
        "__fallback_implementation",
        "name",
    )

    def __init__(
        self,
        name: str,
        connection: Any,
        fallback_implementation: Any,
        canonical_implementation: Any | None = None,
    ) -> None:
        self.name = name
        self.__connection_identity = id(connection)
        self.__fallback_implementation = fallback_implementation
        self.__canonical_implementation = canonical_implementation

    @property
    def connection_identity(self) -> int:
        return self.__connection_identity

    def __getattr__(self, name: str) -> Any:
        if not _is_delegated_operation(self.name, name):
            raise AttributeError(
                f"repository {self.name!r} does not expose UoW capability {name!r}"
            )
        canonical = self.__canonical_implementation
        if canonical is not None:
            try:
                return getattr(canonical, name)
            except AttributeError:
                pass
        return getattr(self.__fallback_implementation, name)

    def __dir__(self) -> list[str]:
        public_local = {name for name in super().__dir__() if not name.startswith("_")}
        delegated = {
            name
            for name in dir(self.__fallback_implementation)
            if _is_delegated_operation(self.name, name)
        }
        canonical = self.__canonical_implementation
        if canonical is not None:
            delegated.update(
                name
                for name in dir(canonical)
                if _is_delegated_operation(self.name, name)
            )
        return sorted(public_local | delegated)


class PostgresRepositoryContext:
    """Bind all repository roles to one exact UoW-owned connection."""

    __slots__ = (
        "__connection_identity",
        "__corpus_repository",
        "__derivation_repository",
        "__repositories",
    )

    def __init__(
        self,
        connection: Any,
        implementation: Any,
        indexing_persistence_error: type[Exception],
    ) -> None:
        self.__connection_identity = id(connection)
        self.__corpus_repository = PostgresCorpusRepository(
            connection,
            embedding_model=implementation.embedding_model,
            embedding_revision=implementation.embedding_revision,
            embedding_dimension=implementation.embedding_dimension,
            indexing_persistence_error=indexing_persistence_error,
        )
        self.__derivation_repository = PostgresDerivationRepository(connection)
        self.__repositories = {}
        for role in REPOSITORY_ROLES:
            canonical = None
            if role in _CORPUS_ROLES:
                canonical = self.__corpus_repository
            elif role == "derivations":
                canonical = self.__derivation_repository
            self.__repositories[role] = PostgresRepositoryView(
                role,
                connection,
                implementation,
                canonical,
            )

    @property
    def connection_identity(self) -> int:
        return self.__connection_identity

    def repository(self, role: str) -> PostgresRepositoryView:
        try:
            return self.__repositories[role]
        except KeyError as exc:
            raise KeyError(f"unknown PostgreSQL repository role: {role}") from exc

    def bind(self, uow: Any) -> None:
        for role, repository in self.__repositories.items():
            setattr(uow, role, repository)

        # Temporary direct-UoW compatibility facade. Instance-bound delegates
        # deliberately override legacy class methods (including issue #217's
        # batch compatibility installer), so execution is owned by the
        # connection-bound repositories while callers migrate incrementally.
        for name in _CORPUS_COMPATIBILITY_OPERATIONS:
            setattr(uow, name, getattr(self.__corpus_repository, name))
        for name in _DERIVATION_COMPATIBILITY_OPERATIONS:
            setattr(uow, name, getattr(self.__derivation_repository, name))


def install_shared_repository_context(postgres_module: Any) -> None:
    """Install canonical Phase-3 repositories without changing UoW ownership."""

    uow_type = postgres_module.PostgresUnitOfWork
    if getattr(uow_type, _INSTALL_MARKER, False):
        return

    original_enter = uow_type.__enter__

    @wraps(original_enter)
    def enter_with_repositories(self: Any) -> Any:
        entered = original_enter(self)
        connection = self.connection
        if connection is None:
            raise RuntimeError("PostgresUnitOfWork entered without a connection")
        repository_context = PostgresRepositoryContext(
            connection,
            self,
            postgres_module.IndexingPersistenceError,
        )
        repository_context.bind(self)
        self._repository_context = repository_context
        return entered

    uow_type.__enter__ = enter_with_repositories
    setattr(uow_type, _INSTALL_MARKER, True)
