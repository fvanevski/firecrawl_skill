from __future__ import annotations

from functools import partial
from typing import Any

from .acquisition_service import AcquisitionService
from .blob import ContentAddressedBlobStore
from .config import StoreConfig
from .extraction_service import ExtractionService
from .indexing import OpenAICompatibleEmbedder
from .legacy_adapter import AdapterMode, LegacyEntryPointAdapter
from .postgres import PostgresUnitOfWork
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
    functions.  When no explicit ``orchestrator_config`` is supplied, a
    ResourceGovernor is built and attached so that synthesis LLM calls are
    bounded through the governor.
    """
    from .orchestrator import OrchestratorConfig, ResearchOrchestrator

    config = config or StoreConfig.from_env()
    if orchestrator_config is None:
        governor = build_resource_governor(config)
        orchestrator_config = OrchestratorConfig(
            resource_governor=governor,
        )
    elif getattr(orchestrator_config, "resource_governor", None) is None:
        # Existing config was passed but no governor — attach one.
        governor = build_resource_governor(config)
        # Rebuild with the governor attached.
        orchestrator_config = OrchestratorConfig(
            execution_mode=getattr(
                orchestrator_config, "execution_mode", "autonomous_local"
            ),
            budget_policy_version=getattr(
                orchestrator_config, "budget_policy_version", "budget-policy-v1"
            ),
            max_adaptive_cycles=getattr(orchestrator_config, "max_adaptive_cycles", 10),
            resume_on_conflict=getattr(orchestrator_config, "resume_on_conflict", True),
            legacy_adapter_mode=getattr(
                orchestrator_config, "legacy_adapter_mode", "authoritative"
            ),
            resource_governor=governor,
        )

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


def build_resource_governor(
    config: StoreConfig | None = None,
) -> Any:
    """Build a ResourceGovernor wired to the PostgreSQL database.

    Args:
        config: Store config. Uses ``StoreConfig.from_env()`` when
            ``None``.

    Returns:
        A ``ResourceGovernor`` instance with PostgreSQL-backed health
        persistence and all endpoint configurations registered.
    """
    config = config or StoreConfig.from_env()
    config.require_database()
    from .resource_governor import (
        EndpointConfig,
        ResourceGovernor,
        make_health_query,
        make_health_store,
    )

    uow_factory = partial(
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

    governor = ResourceGovernor(
        health_store=make_health_store(uow_factory),
        health_query=make_health_query(uow_factory),
    )

    # Register generative endpoint.
    if config.generative_url:
        governor.register_endpoint(
            EndpointConfig(
                name="generative",
                url=config.generative_url,
                max_concurrent=config.generative_max_concurrent,
                max_input_tokens=config.generative_max_input_tokens,
                max_batch_size=config.generative_max_batch_size,
                health_check_interval=config.generative_health_check_interval,
                backpressure_threshold=config.generative_backpressure_threshold,
                token_cap=config.generative_token_cap,
            )
        )

    # Register embedding endpoint.
    if config.embedding_url:
        governor.register_endpoint(
            EndpointConfig(
                name="embedding",
                url=config.embedding_url,
                max_concurrent=config.embedding_max_concurrent,
                max_batch_size=config.embedding_max_batch_size,
                health_check_interval=config.embedding_health_check_interval,
                backpressure_threshold=config.embedding_backpressure_threshold,
            )
        )

    # Register reranker endpoint.
    if config.reranker_url:
        governor.register_endpoint(
            EndpointConfig(
                name="reranker",
                url=config.reranker_url,
                max_concurrent=config.reranker_max_concurrent,
                max_batch_size=config.reranker_max_batch_size,
                health_check_interval=config.reranker_health_check_interval,
                backpressure_threshold=config.reranker_backpressure_threshold,
            )
        )

    return governor


def build_audit_service(config: StoreConfig | None = None):
    """Build an AuditService wired to the PostgreSQL database."""
    # config may be a uow_factory partial from run_service; ignore it and
    # always read a fresh StoreConfig so monkeypatch-ed env vars are picked
    # up by tests.
    config = StoreConfig.from_env()
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


def build_evidence_service(config: StoreConfig | None = None):
    """Build an EvidenceService wired to the PostgreSQL database."""
    config = config or StoreConfig.from_env()
    config.require_database()
    from budget_policy import DEFAULT_POLICY

    from .evidence import EvidenceService

    return EvidenceService(
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
        tokenizer_name=config.tokenizer_name,
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
