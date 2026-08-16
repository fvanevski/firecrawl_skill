"""Shared-connection PostgreSQL repository/UoW seam.

Issue #255 establishes explicit repository objects without moving domain SQL out of
``PostgresUnitOfWork``. Later Phase-3 issues replace these compatibility views
with cohesive repository implementations. Until then each view delegates domain
operations to the existing implementation while carrying only opaque identity for
the exact connection owned by the containing UoW.

The compatibility seam is intentionally capability-filtered: repository views do not
expose connection lifecycle, transaction control, the generic SQL executor/cursor
chain, or unrelated private UoW helpers. The one temporary private exception,
``_lock_workflow_run``, preserves an existing cross-service row-lock contract used by
asset promotion; it operates on the caller-supplied cursor and does not own a
transaction boundary. Issues #256-#259 can retire that compatibility exception when
the corresponding SQL is moved into cohesive repositories.
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

# These are UoW/infrastructure capabilities, not repository operations. In
# particular, execute()/fetchone() must not leak through a repository view:
# execute("COMMIT"), execute("ROLLBACK"), or savepoint SQL would otherwise bypass
# the named transaction-method denylist and violate the UoW authority boundary.
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

# Existing asset-promotion code calls this lock helper through ``uow.runs`` while
# holding a cursor from the containing UoW connection. It is a row-lock operation,
# not an independent transaction capability, and remains explicit/temporary until
# the workflow repository extraction owns the operation directly.
_COMPATIBILITY_HELPERS = frozenset({"_lock_workflow_run"})
_INSTALL_MARKER = "_shared_repository_context_installed"


def _is_delegated_operation(name: str) -> bool:
    """Return whether *name* is part of the temporary repository surface."""
    if name in _COMPATIBILITY_HELPERS:
        return True
    return not name.startswith("_") and name not in _NON_REPOSITORY_CAPABILITIES


class PostgresRepositoryView:
    """Temporary connection-bound repository view for incremental SQL extraction.

    Repository consumers receive domain operations plus explicitly enumerated
    compatibility helpers. The raw connection, UoW implementation object,
    connection lifecycle, transaction controls, generic SQL executor/cursor chain,
    and every other private helper are outside the repository surface. Delegation
    preserves current domain behavior until issues #256-#259 move the corresponding
    SQL into cohesive repositories.
    """

    __slots__ = ("__connection_identity", "__implementation", "name")

    def __init__(self, name: str, connection: Any, implementation: Any) -> None:
        self.name = name
        self.__connection_identity = id(connection)
        self.__implementation = implementation

    @property
    def connection_identity(self) -> int:
        """Opaque identity token for exact-connection diagnostics and regressions."""
        return self.__connection_identity

    def __getattr__(self, name: str) -> Any:
        if not _is_delegated_operation(name):
            raise AttributeError(
                f"repository {self.name!r} does not expose UoW capability {name!r}"
            )
        return getattr(self.__implementation, name)

    def __dir__(self) -> list[str]:
        public_local = {name for name in super().__dir__() if not name.startswith("_")}
        delegated = {
            name for name in dir(self.__implementation) if _is_delegated_operation(name)
        }
        return sorted(public_local | delegated)


class PostgresRepositoryContext:
    """Factory/context binding all repository roles to one UoW connection."""

    __slots__ = ("__connection_identity", "__repositories")

    def __init__(self, connection: Any, implementation: Any) -> None:
        self.__connection_identity = id(connection)
        self.__repositories = {
            role: PostgresRepositoryView(role, connection, implementation)
            for role in REPOSITORY_ROLES
        }

    @property
    def connection_identity(self) -> int:
        """Opaque identity token for the single connection shared by this context."""
        return self.__connection_identity

    def repository(self, role: str) -> PostgresRepositoryView:
        """Return the stable repository view for one declared role."""
        try:
            return self.__repositories[role]
        except KeyError as exc:
            raise KeyError(f"unknown PostgreSQL repository role: {role}") from exc

    def bind(self, uow: Any) -> None:
        """Install each declared repository view on the containing UoW."""
        for role, repository in self.__repositories.items():
            setattr(uow, role, repository)


def install_shared_repository_context(postgres_module: Any) -> None:
    """Install the Phase-3 repository seam on the canonical UoW class in place.

    In-place installation preserves the established ``research_store.postgres``
    import/class identity while later issues incrementally extract SQL. The
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
