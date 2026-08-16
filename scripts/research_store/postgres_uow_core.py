"""Shared-connection PostgreSQL repository/UoW seam.

Issue #255 establishes explicit repository objects without moving domain SQL out of
``PostgresUnitOfWork``. Later Phase-3 issues replace these compatibility views
with cohesive repository implementations. Until then each view delegates existing
public domain operations to the UoW implementation while carrying only opaque
identity for the exact connection owned by the containing UoW.

The compatibility seam is intentionally capability-filtered: repository views do not
expose connection lifecycle, transaction control, the generic SQL executor/cursor
chain, or arbitrary private UoW helpers. Two existing run-repository operations are
explicitly retained only on the ``runs`` view:

* ``_bump_lifecycle_revision`` is already part of the typed
  ``ResearchRunRepository`` contract and performs a CAS-style run mutation inside
  the containing UoW transaction.
* ``_lock_workflow_run`` is a temporary compatibility operation used by established
  workflow/checkpoint code; it operates on a caller-supplied cursor and acquires a
  row lock without opening, committing, rolling back, or nesting a transaction.

Public domain delegation remains compatibility-complete in #255 because moving and
fully partitioning the domain SQL belongs to issues #256-#259. The authority boundary
established here is stricter: no repository role can obtain the raw connection or
invoke UoW transaction/executor infrastructure, and private compatibility operations
are role-scoped rather than globally delegated.
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

# Private operations are never delegated generically. These two are established
# run-repository contracts/call paths and are scoped to the runs view only.
_ROLE_PRIVATE_OPERATIONS: dict[str, frozenset[str]] = {
    "runs": frozenset({"_bump_lifecycle_revision", "_lock_workflow_run"}),
}
_INSTALL_MARKER = "_shared_repository_context_installed"


def _is_delegated_operation(role: str, name: str) -> bool:
    """Return whether *name* belongs to the temporary surface for *role*."""
    if name.startswith("_"):
        return name in _ROLE_PRIVATE_OPERATIONS.get(role, frozenset())
    return name not in _NON_REPOSITORY_CAPABILITIES


class PostgresRepositoryView:
    """Temporary connection-bound repository view for incremental SQL extraction.

    Repository consumers receive existing public domain operations plus only the
    explicitly enumerated private operations for their role. The raw connection,
    UoW implementation object, connection lifecycle, transaction controls, generic
    SQL executor/cursor chain, and every other private helper are outside the
    repository surface. Later Phase-3 issues replace public delegation with cohesive
    repository implementations without changing this one-connection UoW boundary.
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
        if not _is_delegated_operation(self.name, name):
            raise AttributeError(
                f"repository {self.name!r} does not expose UoW capability {name!r}"
            )
        return getattr(self.__implementation, name)

    def __dir__(self) -> list[str]:
        public_local = {name for name in super().__dir__() if not name.startswith("_")}
        delegated = {
            name
            for name in dir(self.__implementation)
            if _is_delegated_operation(self.name, name)
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
