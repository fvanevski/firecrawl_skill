"""Shared-connection PostgreSQL repository/UoW seam.

Issues #255-#259 established one UoW-owned PostgreSQL connection/transaction
boundary and extracted durable-state behavior into explicit connection-bound
repositories.  The final structural topology exposes those repositories only
through named UoW roles; it does not install migration-era domain methods on
the UoW instance and does not multiplex acquisition, candidate, extraction, or
semantic behavior through ``uow.runs``.

The UoW remains the sole owner of connection lifecycle, commit, rollback, and
savepoints. Repository views do not expose transaction control, raw connection
access, or the generic SQL executor.  The separately documented class-level
``persist_ingest`` and issue-#217 ingestion-batch compatibility contracts are
installed outside this module and remain behavioral API exceptions; they are
not generic repository-routing facades.
"""

from __future__ import annotations

import json
from functools import wraps
from typing import Any
from uuid import UUID

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
from .postgres_research import PostgresResearchRepository, _lock_workflow_run
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
from .temporal_candidate import canonical_candidate_temporal

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

_INSTALL_MARKER = "_shared_repository_context_installed"


def _is_delegated_operation(role: str, name: str) -> bool:
    if name.startswith("_"):
        return name in _ROLE_PRIVATE_OPERATIONS.get(role, frozenset())
    return name not in _NON_REPOSITORY_CAPABILITIES


class _CompositeRepository:
    """Resolve one cohesive role across canonical repositories."""

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


class _TemporalCandidateRepository(PostgresCandidateRepository):
    """Canonicalize provider dates before candidate materialization commits."""

    def __init__(self, connection: Any, search_repository: Any) -> None:
        super().__init__(connection, search_repository)
        self._temporal_connection = connection

    def record_response_candidates(
        self, *args: Any, **kwargs: Any
    ) -> list[dict[str, Any]]:
        occurrences = super().record_response_candidates(*args, **kwargs)
        run_id = UUID(str(args[0] if args else kwargs["run_id"]))
        with self._temporal_connection.cursor() as cursor:
            for occurrence in occurrences:
                candidate_id = UUID(str(occurrence["candidate_id"]))
                raw_value = occurrence.get("raw_item") or {}
                raw = dict(raw_value) if isinstance(raw_value, dict) else {}
                cursor.execute(
                    """SELECT published_at,date_signals FROM search_candidates
                         WHERE id=%s AND run_id=%s FOR UPDATE""",
                    (candidate_id, run_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError(
                        f"materialized search candidate disappeared: {candidate_id}"
                    )
                publication, signals = canonical_candidate_temporal(
                    raw,
                    stored_publication=row[0],
                    stored_signals=row[1] or {},
                )
                cursor.execute(
                    """UPDATE search_candidates
                          SET published_at=%s,date_signals=%s::jsonb
                        WHERE id=%s AND run_id=%s""",
                    (
                        publication,
                        json.dumps(signals, sort_keys=True),
                        candidate_id,
                        run_id,
                    ),
                )
        return occurrences


class _RunLockedEvidencePacketRepository(PostgresEvidencePacketRepository):
    """Serialize packet revision writes with lifecycle/terminal authority."""

    def __init__(self, connection: Any) -> None:
        super().__init__(connection)
        self._packet_connection = connection

    def persist_evidence_packet(self, run_id: UUID, *args: Any, **kwargs: Any) -> UUID:
        with self._packet_connection.cursor() as cursor:
            _lock_workflow_run(cursor, run_id)
        return super().persist_evidence_packet(run_id, *args, **kwargs)


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
        self.__candidate_repository = _TemporalCandidateRepository(
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
        self.__evidence_packet_repository = _RunLockedEvidencePacketRepository(
            connection
        )
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
        canonical_by_role = {
            "sources": corpus,
            "snapshots": corpus,
            "documents": corpus,
            "chunks": chunks,
            "runs": self.__research_repository,
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
        """Expose only named repository roles on an entered UoW instance."""
        for role, repository in self.__repositories.items():
            setattr(uow, role, repository)


def install_shared_repository_context(postgres_module: Any) -> None:
    """Install canonical repositories without changing UoW transaction ownership."""

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
