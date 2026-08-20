"""Shared-connection PostgreSQL repository/UoW seam.

Issues #255-#258 established the one-connection transaction boundary and moved
the first persistence families into canonical repositories. Issue #259
completes that extraction: every repository role now has an explicit
connection-bound implementation, and the UoW is only transaction/composition
infrastructure plus temporary compatibility delegates.

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
from .postgres_audit import PostgresAuditRepository
from .postgres_corpus import PostgresCorpusRepository
from .postgres_corpus_queries import PostgresCorpusQueryRepository
from .postgres_coverage import PostgresCoverageRepository
from .postgres_derivations import PostgresDerivationRepository
from .postgres_evidence import (
    PostgresClaimEvidenceRepository,
    PostgresEvidencePacketRepository,
)
from .postgres_research import PostgresResearchRepository
from .postgres_semantic_state import (
    PostgresModelEndpointRepository,
    PostgresSemanticCacheRepository,
    PostgresSemanticCallRepository,
    PostgresSynthesisStageRepository,
)
from .postgres_strategy import PostgresStrategyRevisionRepository
from .postgres_terminal import PostgresTerminalDecisionRepository
from .retrieval.postgres import PostgresRetrievalRepository
from .retrieval.projection.postgres_jobs import PostgresIndexJobRepository

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
    "claims",
    "evidence_packets",
    "audits",
    "semantic_calls",
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
_CORPUS_QUERY_COMPATIBILITY_OPERATIONS = (
    "corpus_overview",
    "search_lexical",
    "inspect_asset",
    "fetch_passages",
    "fetch_run_passages",
    "expand_relationships",
    "chunks_for_index",
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
_RETRIEVAL_COMPATIBILITY_OPERATIONS = (
    "log_retrieval",
    "log_retrieval_batch",
    "get_trace",
    "record_retrieval_execution",
)
_INDEX_JOB_COMPATIBILITY_OPERATIONS = (
    "claim_jobs",
    "renew_job",
    "count_complete_manifests",
    "finish_job",
    "heartbeat_worker",
    "worker_status",
    "census_index_jobs",
)
_CLAIM_COMPATIBILITY_OPERATIONS = (
    "upsert_claim",
    "list_claims",
    "delete_claims",
    "validate_passage_id",
    "validate_snapshot_id",
    "validate_claim_id",
    "insert_evidence_link",
    "list_evidence_links",
    "delete_evidence_links",
    "export_claim_manifest",
)
_EVIDENCE_PACKET_COMPATIBILITY_OPERATIONS = (
    "persist_evidence_packet",
    "get_evidence_packet",
)
_AUDIT_COMPATIBILITY_OPERATIONS = (
    "create_audit_assessment",
    "get_audit_assessment",
    "list_audit_assessments",
    "detect_stale_assessments",
    "export_audit_assessment",
    "insert_audit_stage_output",
    "list_audit_stage_outputs",
    "validate_assessment_exists",
    "run_exists",
    "invocation_exists",
    "validate_evidence_references",
    "validate_audit_target",
    "lookup_equivalent_assessment",
    "insert_audit_assessment_if_absent",
)
_SEMANTIC_CALL_COMPATIBILITY_OPERATIONS = (
    "record_semantic_call",
    "finalize_semantic_call",
    "annotate_semantic_call",
    "get_semantic_call",
    "record_semantic_artifact",
)
_SEMANTIC_CACHE_COMPATIBILITY_OPERATIONS = (
    "get_cache_entry_by_key",
    "insert_cache_entry",
    "prune_cache_entries",
    "invalidate_cache_entry",
    "invalidate_cache_entry_by_id",
    "update_cache_entry",
)
_MODEL_ENDPOINT_COMPATIBILITY_OPERATIONS = (
    "upsert_health",
    "get_health",
    "list_endpoints",
    "clear_endpoint_health",
)
_SYNTHESIS_COMPATIBILITY_OPERATIONS = (
    "get_synthesis_stages",
    "get_synthesis_stage",
    "insert_synthesis_stage",
    "update_synthesis_stage",
)
_INSTALL_MARKER = "_shared_repository_context_installed"


def _is_delegated_operation(role: str, name: str) -> bool:
    if name.startswith("_"):
        return name in _ROLE_PRIVATE_OPERATIONS.get(role, frozenset())
    return name not in _NON_REPOSITORY_CAPABILITIES


class _CompositeRepository:
    """Resolve one role across canonical repositories without a UoW fallback."""

    __slots__ = ("__implementations",)

    def __init__(self, *implementations: Any) -> None:
        self.__implementations = implementations

    def __getattr__(self, name: str) -> Any:
        for implementation in self.__implementations:
            try:
                return getattr(implementation, name)
            except AttributeError:
                continue
        raise AttributeError(name)

    def __dir__(self) -> list[str]:
        names = set(super().__dir__())
        for implementation in self.__implementations:
            names.update(dir(implementation))
        return sorted(names)


class _RunsRepository:
    """Canonical run repository plus explicit cross-domain compatibility facades.

    Acquisition and semantic methods historically exposed through ``uow.runs``
    remain UoW-bound wrappers for compatibility. Each wrapper's ``__wrapped__``
    identifies its connection-bound repository; no generic UoW persistence
    fallback is retained.
    """

    __slots__ = ("__legacy_repositories", "__research", "__uow")

    def __init__(
        self,
        uow: Any,
        research: Any,
        legacy_repositories: dict[str, Any],
    ) -> None:
        self.__uow = uow
        self.__research = research
        self.__legacy_repositories = legacy_repositories

    def __getattr__(self, name: str) -> Any:
        try:
            return getattr(self.__research, name)
        except AttributeError:
            pass
        try:
            repository = self.__legacy_repositories[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        return _make_uow_compatibility_delegate(self.__uow, repository, name)

    def __dir__(self) -> list[str]:
        return sorted(
            set(super().__dir__())
            | set(dir(self.__research))
            | set(self.__legacy_repositories)
        )


class PostgresRepositoryView:
    """Capability-filtered view over one explicit canonical implementation."""

    __slots__ = ("__canonical_implementation", "__connection_identity", "name")

    def __init__(
        self,
        name: str,
        connection: Any,
        canonical_implementation: Any,
    ) -> None:
        self.name = name
        self.__connection_identity = id(connection)
        self.__canonical_implementation = canonical_implementation

    @property
    def connection_identity(self) -> int:
        return self.__connection_identity

    def __getattr__(self, name: str) -> Any:
        if not _is_delegated_operation(self.name, name):
            raise AttributeError(
                f"repository {self.name!r} does not expose UoW capability {name!r}"
            )
        try:
            return getattr(self.__canonical_implementation, name)
        except AttributeError as exc:
            raise AttributeError(
                f"repository {self.name!r} has no operation {name!r}"
            ) from exc

    def __dir__(self) -> list[str]:
        public_local = {name for name in super().__dir__() if not name.startswith("_")}
        delegated = {
            name
            for name in dir(self.__canonical_implementation)
            if _is_delegated_operation(self.name, name)
        }
        return sorted(public_local | delegated)


def _make_uow_compatibility_delegate(uow: Any, repository: Any, name: str) -> Any:
    """Create a no-SQL UoW-bound facade for one canonical repository method."""

    canonical_method = getattr(repository, name)

    @wraps(canonical_method)
    def compatibility_method(_uow: Any, *args: Any, **kwargs: Any) -> Any:
        return canonical_method(*args, **kwargs)

    return MethodType(compatibility_method, uow)


def _bind_uow_compatibility_delegate(uow: Any, repository: Any, name: str) -> None:
    """Bind a campaign-required UoW facade to one canonical repository method."""

    setattr(uow, name, _make_uow_compatibility_delegate(uow, repository, name))


class PostgresRepositoryContext:
    """Bind every repository role to one exact UoW-owned connection."""

    __slots__ = (
        "__audit_repository",
        "__candidate_repository",
        "__claim_repository",
        "__connection_identity",
        "__corpus_query_repository",
        "__corpus_repository",
        "__coverage_repository",
        "__derivation_repository",
        "__evidence_packet_repository",
        "__extraction_attempt_repository",
        "__index_job_repository",
        "__model_endpoint_repository",
        "__repositories",
        "__research_repository",
        "__retrieval_repository",
        "__search_acquisition_repository",
        "__semantic_cache_repository",
        "__semantic_call_repository",
        "__strategy_repository",
        "__synthesis_repository",
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
        self.__corpus_query_repository = PostgresCorpusQueryRepository(
            connection,
            parser_version=implementation.parser_version,
            normalization_version=implementation.normalization_version,
            chunker_version=implementation.chunker_version,
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
        self.__retrieval_repository = PostgresRetrievalRepository(connection)
        self.__index_job_repository = PostgresIndexJobRepository(connection)
        self.__claim_repository = PostgresClaimEvidenceRepository(connection)
        self.__evidence_packet_repository = PostgresEvidencePacketRepository(connection)
        self.__audit_repository = PostgresAuditRepository(connection)
        self.__semantic_call_repository = PostgresSemanticCallRepository(
            connection, getattr(implementation, "_telemetry_service", None)
        )
        self.__semantic_cache_repository = PostgresSemanticCacheRepository(connection)
        self.__model_endpoint_repository = PostgresModelEndpointRepository(connection)
        self.__synthesis_repository = PostgresSynthesisStageRepository(connection)

        corpus = _CompositeRepository(
            self.__corpus_repository, self.__corpus_query_repository
        )
        chunks = _CompositeRepository(
            self.__corpus_repository,
            self.__corpus_query_repository,
            self.__derivation_repository,
        )
        runs_legacy_repositories = {
            name: repository
            for repository, operations in (
                (
                    self.__search_acquisition_repository,
                    _ACQUISITION_COMPATIBILITY_OPERATIONS,
                ),
                (self.__candidate_repository, _CANDIDATE_COMPATIBILITY_OPERATIONS),
                (
                    self.__extraction_attempt_repository,
                    _EXTRACTION_COMPATIBILITY_OPERATIONS,
                ),
                (
                    self.__semantic_call_repository,
                    _SEMANTIC_CALL_COMPATIBILITY_OPERATIONS,
                ),
            )
            for name in operations
        }
        runs = _RunsRepository(
            implementation,
            self.__research_repository,
            runs_legacy_repositories,
        )
        canonical_by_role = {
            "sources": corpus,
            "snapshots": corpus,
            "documents": corpus,
            "chunks": chunks,
            "runs": runs,
            "retrieval_events": self.__retrieval_repository,
            "index_jobs": self.__index_job_repository,
            "search_responses": self.__search_acquisition_repository,
            "candidates": self.__candidate_repository,
            "strategy_revisions": self.__strategy_repository,
            "coverage": self.__coverage_repository,
            "terminal_decisions": self.__terminal_repository,
            "extraction_attempts": self.__extraction_attempt_repository,
            "derivations": self.__derivation_repository,
            "claims": self.__claim_repository,
            "evidence_packets": self.__evidence_packet_repository,
            "audits": self.__audit_repository,
            "semantic_calls": self.__semantic_call_repository,
            "semantic_cache": self.__semantic_cache_repository,
            "model_endpoints": self.__model_endpoint_repository,
            "synthesis_stages": self.__synthesis_repository,
        }
        self.__repositories = {
            role: PostgresRepositoryView(role, connection, canonical_by_role[role])
            for role in REPOSITORY_ROLES
        }

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

        direct_compatibility_sets = (
            (self.__corpus_repository, _CORPUS_COMPATIBILITY_OPERATIONS),
            (self.__corpus_query_repository, _CORPUS_QUERY_COMPATIBILITY_OPERATIONS),
            (self.__research_repository, _RESEARCH_COMPATIBILITY_OPERATIONS),
            (self.__coverage_repository, _COVERAGE_COMPATIBILITY_OPERATIONS),
            (self.__strategy_repository, _STRATEGY_COMPATIBILITY_OPERATIONS),
            (self.__terminal_repository, _TERMINAL_COMPATIBILITY_OPERATIONS),
            (self.__derivation_repository, _DERIVATION_COMPATIBILITY_OPERATIONS),
            (self.__retrieval_repository, _RETRIEVAL_COMPATIBILITY_OPERATIONS),
            (self.__index_job_repository, _INDEX_JOB_COMPATIBILITY_OPERATIONS),
            (self.__claim_repository, _CLAIM_COMPATIBILITY_OPERATIONS),
            (
                self.__evidence_packet_repository,
                _EVIDENCE_PACKET_COMPATIBILITY_OPERATIONS,
            ),
            (self.__audit_repository, _AUDIT_COMPATIBILITY_OPERATIONS),
            (self.__semantic_call_repository, _SEMANTIC_CALL_COMPATIBILITY_OPERATIONS),
            (
                self.__semantic_cache_repository,
                _SEMANTIC_CACHE_COMPATIBILITY_OPERATIONS,
            ),
            (
                self.__model_endpoint_repository,
                _MODEL_ENDPOINT_COMPATIBILITY_OPERATIONS,
            ),
            (self.__synthesis_repository, _SYNTHESIS_COMPATIBILITY_OPERATIONS),
        )
        for repository, operations in direct_compatibility_sets:
            for name in operations:
                setattr(uow, name, getattr(repository, name))

        # Preserve only the acquisition-era UoW-bound wrapper shape required by
        # the current campaign. The wrappers contain no SQL and identify their
        # canonical repository via ``__wrapped__``.
        for repository, operations in (
            (
                self.__search_acquisition_repository,
                _ACQUISITION_COMPATIBILITY_OPERATIONS,
            ),
            (self.__candidate_repository, _CANDIDATE_COMPATIBILITY_OPERATIONS),
            (
                self.__extraction_attempt_repository,
                _EXTRACTION_COMPATIBILITY_OPERATIONS,
            ),
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
