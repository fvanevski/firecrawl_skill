from __future__ import annotations

from functools import partial

from .acquisition_service import AcquisitionService
from .blob import ContentAddressedBlobStore
from .config import StoreConfig
from .extraction_service import ExtractionService
from .postgres import PostgresUnitOfWork
from .indexing import OpenAICompatibleEmbedder
from .legacy_adapter import AdapterMode, LegacyEntryPointAdapter
from .qdrant import QdrantIndex
from .queue import ValkeyQueue
from .retrieval import CohereCompatibleReranker
from .run_service import ResearchRunService
from .semantic_service import SemanticCallService
from .service import CorpusService
from .strategy_service import StrategyRevisionService


def build_service(config: StoreConfig | None = None) -> CorpusService:
    config = config or StoreConfig.from_env()
    config.require_database()
    from .parsing import get_registry

    embedder = (
        OpenAICompatibleEmbedder(
            config.embedding_url,
            config.embedding_model,
            config.embedding_api_key,
            config.embedding_dimension,
            config.embedding_fingerprint,
        )
        if config.embedding_url
        else None
    )
    index = QdrantIndex(
        config.qdrant_url,
        config.qdrant_api_key,
        config.qdrant_alias,
        config.embedding_dimension,
    )
    reranker = (
        CohereCompatibleReranker(
            config.reranker_url, config.reranker_model, config.reranker_api_key
        )
        if config.reranker_url
        else None
    )
    return CorpusService(
        config,
        partial(
            PostgresUnitOfWork,
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        ),
        ContentAddressedBlobStore(config.blob_root),
        index=index,
        embedder=embedder,
        reranker=reranker,
        queue=ValkeyQueue(config.valkey_url),
        parser_registry=get_registry(),
    )


def build_run_service(config: StoreConfig | None = None) -> ResearchRunService:
    config = config or StoreConfig.from_env()
    config.require_database()
    return ResearchRunService(
        partial(
            PostgresUnitOfWork,
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        ),
        blob_store=ContentAddressedBlobStore(config.blob_root),
    )


def build_semantic_service(config: StoreConfig | None = None) -> SemanticCallService:
    config = config or StoreConfig.from_env()
    config.require_database()
    return SemanticCallService(
        partial(
            PostgresUnitOfWork,
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        )
    )


def build_acquisition_service(
    config: StoreConfig | None = None, search_adapter=None
) -> AcquisitionService:
    config = config or StoreConfig.from_env()
    config.require_database()
    return AcquisitionService(
        partial(
            PostgresUnitOfWork,
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        ),
        blob_store=ContentAddressedBlobStore(config.blob_root),
        search_adapter=search_adapter,
    )


def build_compatibility_export_service(config: StoreConfig | None = None):
    from .compat_export import SearchCompatibilityExporter

    config = config or StoreConfig.from_env()
    config.require_database()
    return SearchCompatibilityExporter(
        partial(
            PostgresUnitOfWork,
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        ),
        blob_store=ContentAddressedBlobStore(config.blob_root),
    )


def build_legacy_adapter(
    mode: AdapterMode, config: StoreConfig | None = None
) -> LegacyEntryPointAdapter:

    if mode == AdapterMode.COMPATIBILITY:
        return LegacyEntryPointAdapter(None, mode)
    config = config or StoreConfig.from_env()
    config.require_database()
    return LegacyEntryPointAdapter(
        partial(
            PostgresUnitOfWork,
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        ),
        mode,
    )


def build_strategy_service(
    config: StoreConfig | None = None,
) -> StrategyRevisionService:
    config = config or StoreConfig.from_env()
    config.require_database()
    from budget_policy import DEFAULT_POLICY

    return StrategyRevisionService(
        partial(
            PostgresUnitOfWork,
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        ),
        budget_policy=DEFAULT_POLICY,
    )


def build_orchestrator(
    config: StoreConfig | None = None,
    *,
    orchestrator_config=None,
):
    """Build a fully wired ResearchOrchestrator.

    This is a convenience wrapper around ``ResearchOrchestrator.build``
    that uses the same configuration pattern as the other ``build_*``
    functions.
    """
    from .orchestrator import ResearchOrchestrator

    return ResearchOrchestrator.build(config, orchestrator_config=orchestrator_config)


def build_claim_service(config: StoreConfig | None = None):
    """Build a ClaimManifestService wired to the PostgreSQL database."""
    config = config or StoreConfig.from_env()
    config.require_database()
    from .service import ClaimManifestService

    return ClaimManifestService(
        partial(
            PostgresUnitOfWork,
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        )
    )


def build_audit_service(config: StoreConfig | None = None):
    """Build an AuditService wired to the PostgreSQL database."""
    config = config or StoreConfig.from_env()
    config.require_database()
    from .service import AuditService

    return AuditService(
        partial(
            PostgresUnitOfWork,
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        )
    )


def build_catalog_export_service(config: StoreConfig | None = None):
    """Build a Catalog v5 compatibility exporter.

    Args:
        config: Store config. Uses ``StoreConfig.from_env()`` when
            ``None``.

    Returns:
        A ``CatalogExportService`` instance wired to the configured
        PostgreSQL connection and blob store.
    """
    from .blob import ContentAddressedBlobStore
    from .catalog_export import CatalogExportService
    from .postgres import PostgresUnitOfWork

    config = config or StoreConfig.from_env()
    config.require_database()
    return CatalogExportService(
        partial(
            PostgresUnitOfWork,
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        ),
        blob_store=ContentAddressedBlobStore(config.blob_root),
    )


def build_catalog_import_service(config: StoreConfig | None = None):
    """Build a CatalogImportService wired to the PostgreSQL database.

    Args:
        config: Store config. Uses ``StoreConfig.from_env()`` when
            ``None``.

    Returns:
        A ``CatalogImportService`` instance wired to the configured
        PostgreSQL connection.
    """
    from .catalog_import import CatalogImportService
    from .postgres import PostgresUnitOfWork

    config = config or StoreConfig.from_env()
    config.require_database()
    return CatalogImportService(
        partial(
            PostgresUnitOfWork,
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        )
    )


def build_extraction_service(config: StoreConfig | None = None):
    """Build an ExtractionService wired to the PostgreSQL database.

    Args:
        config: Store config. Uses ``StoreConfig.from_env()`` when
            ``None``.

    Returns:
        An ``ExtractionService`` instance wired to the configured
        PostgreSQL connection and blob store.
    """
    config = config or StoreConfig.from_env()
    config.require_database()
    return ExtractionService(
        partial(
            PostgresUnitOfWork,
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        ),
        blob_store=ContentAddressedBlobStore(config.blob_root),
        config=config,
    )
