"""Shared-connection PostgreSQL repository/UoW seam.

Issue #255 establishes explicit repository objects without moving domain SQL out of
``PostgresUnitOfWork``.  Later Phase-3 issues replace these compatibility views
with cohesive repository implementations.  Until then each view delegates domain
operations to the existing implementation while carrying the exact connection
owned by the containing UoW.
"""

from __future__ import annotations

from functools import wraps
from typing import Any

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

_TRANSACTION_CONTROL = frozenset(
    {
        "connection",
        "commit",
        "rollback",
        "savepoint",
        "close",
        "__enter__",
        "__exit__",
    }
)
_INSTALL_MARKER = "_shared_repository_context_installed"


class PostgresRepositoryView:
    """Temporary connection-bound repository view for incremental SQL extraction.

    The raw connection is intentionally private.  Repository consumers receive no
    connection-lifecycle or transaction-control surface; those operations remain
    owned by ``PostgresUnitOfWork``.  Delegation preserves current domain behavior
    until issues #256-#259 move the corresponding SQL into cohesive repositories.
    """

    __slots__ = ("name", "_connection", "_implementation")

    def __init__(self, name: str, connection: Any, implementation: Any) -> None:
        self.name = name
        self._connection = connection
        self._implementation = implementation

    @property
    def connection_identity(self) -> int:
        """Opaque identity token for exact-connection diagnostics and regressions."""
        return id(self._connection)

    def __getattr__(self, name: str) -> Any:
        if name in _TRANSACTION_CONTROL:
            raise AttributeError(
                f"repository {self.name!r} does not own transaction operation {name!r}"
            )
        return getattr(self._implementation, name)

    def __dir__(self) -> list[str]:
        delegated = set(dir(self._implementation)) - _TRANSACTION_CONTROL
        return sorted(set(super().__dir__()) | delegated)


class PostgresRepositoryContext:
    """Factory/context binding all repository roles to one UoW connection."""

    __slots__ = ("_connection", "_implementation", "_repositories")

    def __init__(self, connection: Any, implementation: Any) -> None:
        self._connection = connection
        self._implementation = implementation
        self._repositories = {
            role: PostgresRepositoryView(role, connection, implementation)
            for role in REPOSITORY_ROLES
        }

    @property
    def connection_identity(self) -> int:
        """Opaque identity token for the single connection shared by this context."""
        return id(self._connection)

    def repository(self, role: str) -> PostgresRepositoryView:
        """Return the stable repository view for one declared role."""
        try:
            return self._repositories[role]
        except KeyError as exc:
            raise KeyError(f"unknown PostgreSQL repository role: {role}") from exc

    def bind(self, uow: Any) -> None:
        """Install each declared repository view on the containing UoW."""
        for role, repository in self._repositories.items():
            setattr(uow, role, repository)


def install_shared_repository_context(postgres_module: Any) -> None:
    """Install the Phase-3 repository seam on the canonical UoW class in place.

    In-place installation preserves the established ``research_store.postgres``
    import/class identity while later issues incrementally extract SQL.  The
    original ``__enter__`` remains responsible for opening the connection; this
    wrapper only replaces its historical ``role = self`` aliases after the
    connection exists.
    """

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
        repository_context = PostgresRepositoryContext(connection, self)
        repository_context.bind(self)
        self._repository_context = repository_context
        return entered

    uow_type.__enter__ = enter_with_repositories
    setattr(uow_type, _INSTALL_MARKER, True)
