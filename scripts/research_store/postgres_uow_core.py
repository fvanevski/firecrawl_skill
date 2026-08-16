"""Shared-connection PostgreSQL repository/UoW seam.

Issue #255 established explicit repository views and the one-connection
transaction boundary. Issues #256-#258 replace corpus/derivation, research
workflow, and acquisition persistence with canonical connection-bound
repositories while retaining temporary compatibility delegation for later
Phase-3 domains.

The UoW remains the sole owner of connection lifecycle, commit, rollback, and
savepoints. Repository views do not expose transaction control, raw connection
access, or the generic SQL executor.
"""

from __future__ import annotations

from functools import wraps
from types import MethodType
from typing import Any

from .postgres_acquisition import (
    PostgresCandidateRepository,
    PostgresExtractionAttemptRepository,
    PostgresSearchAcquisitionRepository,
)
from .postgres_corpus import PostgresCorpusRepository
from .postgres_coverage import PostgresCoverageRepository
from .postgres_derivations import PostgresDerivationRepository
from .postgres_research import PostgresResearchRepository
from .postgres_strategy import PostgresStrategyRevisionRepository
from .postgres_terminal import PostgresTerminalDecisionRepository

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
_RESEARCH_COMPATIBILITY_OPERATIONS = (
    "start_run",
    "get_run_status",
    "append_run_transition",
    "apply_run_transition",
    "revise_execution_mode",
    "record_invocation",
    "get_invocation_status",
    "list_invocations",
    "append_event",
    "get_event_by_id",
    "list_events",
    "next_event_sequence",
    "record_research_spec",
    "record_budget_snapshot",
    "get_research_spec",
)
_ACQUISITION_COMPATIBILITY_OPERATIONS = (
    "record_search_plan",
    "get_search_plan",
    "list_search_plans",
    "get_plan_query",
    "list_plan_queries",
    "record_search_response",
    "get_search_response",
    "list_search_responses",
    "open_raw_search_response_blob",
)
_CANDIDATE_COMPATIBILITY_OPERATIONS = (
    "record_response_candidates",
    "get_candidate",
    "list_candidates",
    "list_candidates_paginated",
    "list_candidate_occurrences",
    "assign_duplicate_group",
    "persist_duplicate_group",
    "update_candidate_independence",
    "record_rankings",
)
_EXTRACTION_COMPATIBILITY_OPERATIONS = (
    "create_attempt",
    "complete_attempt",
    "update_disposition",
    "record_quality_metrics",
    "select_final_attempt",
    "get_selected_attempt",
    "list_attempts_for_candidate",
    "list_attempts_for_run",
    "get_attempt",
)
_COVERAGE_COMPATIBILITY_OPERATIONS = (
    "create_items",
    "apply_event",
    "rebuild_projection",
    "create_snapshot",
    "get_snapshot",
    "get_latest_snapshot",
    "list_coverage_events",
    "get_event",
    "get_current_revision",
    "count_events",
    "count_coverage_items",
    "get_coverage_summary",
)
_STRATEGY_COMPATIBILITY_OPERATIONS = (
    "record_proposal",
    "get_proposal",
    "list_proposals",
    "record_decision",
    "get_decision",
    "list_decisions",
    "proposal_exists",
    "get_proposal_by_idempotency",
    "decision_exists",
    "list_proposal_ids_for_run",
    "list_decision_ids_for_proposal",
)
_TERMINAL_COMPATIBILITY_OPERATIONS = ("record_terminal_decision",)
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


def _bind_uow_compatibility_delegate(uow: Any, repository: Any, name: str) -> None:
    """Keep the legacy UoW method shape while delegating to one canonical method.

    Acquisition methods historically flowed through ``uow.runs`` and directly
    through ``uow``. Binding this narrow wrapper to the UoW preserves that
    compatibility surface while ``__wrapped__`` identifies the sole canonical
    connection-bound implementation.
    """

    canonical_method = getattr(repository, name)

    @wraps(canonical_method)
    def compatibility_method(_uow: Any, *args: Any, **kwargs: Any) -> Any:
        return canonical_method(*args, **kwargs)

    setattr(uow, name, MethodType(compatibility_method, uow))


class PostgresRepositoryContext:
    """Bind all repository roles to one exact UoW-owned connection."""

    __slots__ = (
        "__candidate_repository",
        "__connection_identity",
        "__corpus_repository",
        "__coverage_repository",
        "__derivation_repository",
        "__extraction_attempt_repository",
        "__repositories",
        "__research_repository",
        "__search_acquisition_repository",
        "__strategy_repository",
        "__terminal_repository",
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
        self.__research_repository = PostgresResearchRepository(connection)
        self.__search_acquisition_repository = PostgresSearchAcquisitionRepository(
            connection
        )
        self.__candidate_repository = PostgresCandidateRepository(
            connection, self.__search_acquisition_repository
        )
        self.__extraction_attempt_repository = PostgresExtractionAttemptRepository(
            connection
        )
        self.__coverage_repository = PostgresCoverageRepository(connection)
        self.__strategy_repository = PostgresStrategyRevisionRepository(connection)
        self.__terminal_repository = PostgresTerminalDecisionRepository(connection)
        self.__derivation_repository = PostgresDerivationRepository(connection)
        self.__repositories = {}
        for role in REPOSITORY_ROLES:
            canonical = None
            if role in _CORPUS_ROLES:
                canonical = self.__corpus_repository
            elif role == "runs":
                canonical = self.__research_repository
            elif role == "search_responses":
                canonical = self.__search_acquisition_repository
            elif role == "candidates":
                canonical = self.__candidate_repository
            elif role == "extraction_attempts":
                canonical = self.__extraction_attempt_repository
            elif role == "coverage":
                canonical = self.__coverage_repository
            elif role == "strategy_revisions":
                canonical = self.__strategy_repository
            elif role == "terminal_decisions":
                canonical = self.__terminal_repository
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
        # deliberately override legacy class methods so execution is owned by
        # connection-bound repositories while callers migrate incrementally.
        compatibility_sets = (
            (self.__corpus_repository, _CORPUS_COMPATIBILITY_OPERATIONS),
            (self.__research_repository, _RESEARCH_COMPATIBILITY_OPERATIONS),
            (self.__coverage_repository, _COVERAGE_COMPATIBILITY_OPERATIONS),
            (self.__strategy_repository, _STRATEGY_COMPATIBILITY_OPERATIONS),
            (self.__terminal_repository, _TERMINAL_COMPATIBILITY_OPERATIONS),
            (self.__derivation_repository, _DERIVATION_COMPATIBILITY_OPERATIONS),
        )
        for repository, operations in compatibility_sets:
            for name in operations:
                setattr(uow, name, getattr(repository, name))

        # Acquisition keeps the legacy ``uow``/``uow.runs`` method binding
        # shape for this campaign phase, but the wrapper has no persistence of
        # its own: every call is forwarded to the sole canonical repository.
        for repository, operations in (
            (self.__search_acquisition_repository, _ACQUISITION_COMPATIBILITY_OPERATIONS),
            (self.__candidate_repository, _CANDIDATE_COMPATIBILITY_OPERATIONS),
            (self.__extraction_attempt_repository, _EXTRACTION_COMPATIBILITY_OPERATIONS),
        ):
            for name in operations:
                _bind_uow_compatibility_delegate(uow, repository, name)


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
