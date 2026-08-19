"""Canonical production composition root for the research store.

This module is the only production package surface that constructs concrete
unit-of-work factories, infrastructure adapters, and application services from
``StoreConfig``.  It owns wiring only: deterministic policy, persistence
semantics, workflow decisions, and transaction behavior remain in their
respective services and repositories.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from .acquisition.adapters.bounded_firecrawl import BoundedFirecrawlSearchAdapter
from .acquisition.authority import require_authoritative_acquisition
from .acquisition.service import AcquisitionService
from .blob import ContentAddressedBlobStore
from .bounded_orchestrator import BoundedAcquisitionStage, BoundedExtractionStage
from .checkpoint_orchestrator import CheckpointResearchOrchestrator
from .config import StoreConfig
from .corpus_service import CorpusService
from .extraction_service import ExtractionService
from .indexing import OpenAICompatibleEmbedder
from .lifecycle_guard import GuardedResearchRunService as ResearchRunService
from .orchestrator import OrchestratorConfig, ResearchOrchestrator
from .postgres import PostgresUnitOfWork
from .qdrant import QdrantIndex
from .retrieval import CohereCompatibleReranker
from .semantic_service import SemanticCallService
from .strategy_service import StrategyRevisionService
from .valkey_queue import ValkeyQueue

if TYPE_CHECKING:
    from .acquisition.direct_scrape import DirectScrapeService
    from .acquisition.ports import DirectScrapeAdapter

UowFactory = Callable[[], PostgresUnitOfWork]


def build_uow_factory(config: StoreConfig) -> UowFactory:
    """Bind the canonical PostgreSQL unit-of-work constructor to ``config``.

    Deliberately does not call ``require_database``: historical low-level
    factory surfaces only bound constructor arguments.  Public service builders
    retain their existing fail-fast database validation before composition.
    """
    return partial(
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
        build_uow_factory(config),
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
        build_uow_factory(config),
        blob_store=ContentAddressedBlobStore(config.blob_root),
    )


def build_invocation_service(config: StoreConfig | None = None):
    """Build invocation persistence with exact locked lifecycle provenance."""
    from .direct_invocation_service import DirectInvocationService

    run_service = build_run_service(config)
    return DirectInvocationService(run_service.uow_factory)


def build_workflow_operation_service(config: StoreConfig | None = None):
    """Build checkpoint-guarded wrapper boundaries over PostgreSQL state."""
    from .checkpoint_workflow_service import CheckpointWorkflowOperationService

    run_service = build_run_service(config)
    return CheckpointWorkflowOperationService(
        run_service,
        build_invocation_service(config),
    )


def build_semantic_service(config: StoreConfig | None = None) -> SemanticCallService:
    config = config or StoreConfig.from_env()
    config.require_database()
    return SemanticCallService(build_uow_factory(config))


def build_acquisition_service(
    config: StoreConfig | None = None, search_adapter=None
) -> AcquisitionService:
    """Compose acquisition policy with an explicit provider adapter."""
    config = config or StoreConfig.from_env()
    config.require_database()
    adapter = (
        search_adapter
        if search_adapter is not None
        else BoundedFirecrawlSearchAdapter()
    )
    return AcquisitionService(
        build_uow_factory(config),
        blob_store=ContentAddressedBlobStore(config.blob_root),
        search_adapter=adapter,
        config=config,
        authority_preflight=require_authoritative_acquisition,
    )


def build_strategy_service(
    config: StoreConfig | None = None,
) -> StrategyRevisionService:
    config = config or StoreConfig.from_env()
    config.require_database()
    from budget_policy import DEFAULT_POLICY

    return StrategyRevisionService(
        build_uow_factory(config),
        budget_policy=DEFAULT_POLICY,
    )


def build_claim_service(config: StoreConfig | None = None):
    """Build a ClaimManifestService wired to the PostgreSQL database."""
    config = config or StoreConfig.from_env()
    config.require_database()
    from .assessment.claims import ClaimManifestService

    return ClaimManifestService(build_uow_factory(config))


def build_resource_governor(
    config: StoreConfig | None = None,
) -> Any:
    """Build a ResourceGovernor wired to configured persistence/endpoints."""
    config = config or StoreConfig.from_env()
    config.require_database()
    from .resource_governor import (
        EndpointConfig,
        ResourceGovernor,
        make_health_query,
        make_health_store,
    )

    uow_factory = build_uow_factory(config)
    governor = ResourceGovernor(
        health_store=make_health_store(uow_factory),
        health_query=make_health_query(uow_factory),
    )

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


def build_audit_service(config: StoreConfig | Any | None = None):
    """Build an AuditService from a store config or existing UoW factory."""
    from .assessment.audit import AuditService

    if config is not None and not isinstance(config, StoreConfig):
        return AuditService(config)

    resolved = config or StoreConfig.from_env()
    resolved.require_database()
    return AuditService(build_uow_factory(resolved))


def build_evidence_service(config: StoreConfig | None = None):
    """Build an EvidenceService wired to the PostgreSQL database."""
    config = config or StoreConfig.from_env()
    config.require_database()
    from budget_policy import DEFAULT_POLICY

    from .evidence import EvidenceService

    return EvidenceService(
        build_uow_factory(config),
        budget_policy=DEFAULT_POLICY,
        tokenizer_name=config.tokenizer_name,
    )


def build_extraction_service(config: StoreConfig | None = None):
    """Build an ExtractionService wired to PostgreSQL and the blob store."""
    config = config or StoreConfig.from_env()
    config.require_database()
    return ExtractionService(
        build_uow_factory(config),
        blob_store=ContentAddressedBlobStore(config.blob_root),
        config=config,
    )


def build_direct_scrape_service(
    config: StoreConfig | None = None,
    *,
    adapter_factory: Callable[[], DirectScrapeAdapter] | None = None,
) -> DirectScrapeService:
    """Compose the authoritative direct-scrape service and provider adapter."""
    from .acquisition.direct_scrape import DirectScrapeService
    from .parsing import get_registry

    if adapter_factory is None:
        from .acquisition.adapters.firecrawl_scrape import FirecrawlDirectScrapeAdapter

        adapter_factory = FirecrawlDirectScrapeAdapter

    resolved = config or StoreConfig.from_env()
    resolved.require_database()
    uow_factory = build_uow_factory(resolved)
    blob_store = ContentAddressedBlobStore(resolved.blob_root)
    corpus_service = CorpusService(
        resolved,
        uow_factory,
        blob_store,
        parser_registry=get_registry(),
    )
    return DirectScrapeService(
        resolved,
        uow_factory,
        blob_store,
        corpus_service,
        adapter_factory=adapter_factory,
    )


class ProductionBoundedExtractionStage(BoundedExtractionStage):
    """Production bounded extraction with explicit Firecrawl composition."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("scrape_adapter", BoundedFirecrawlSearchAdapter())
        super().__init__(*args, **kwargs)


def build_production_orchestrator(
    config: StoreConfig,
    *,
    orchestrator_config: OrchestratorConfig | None = None,
) -> ResearchOrchestrator:
    """Build the fresh production orchestrator with bounded stages."""
    return CheckpointResearchOrchestrator.build(
        config,
        orchestrator_config=orchestrator_config,
        acquisition_stage_cls=BoundedAcquisitionStage,
        extraction_stage_cls=ProductionBoundedExtractionStage,
    )


def build_production_resumable_orchestrator(
    config: StoreConfig,
    *,
    orchestrator_config: OrchestratorConfig | None = None,
):
    """Build the production smart-resume orchestrator explicitly."""
    from .search_provenance import ProvenanceResumableResearchOrchestrator

    return ProvenanceResumableResearchOrchestrator.build(
        config,
        orchestrator_config=orchestrator_config,
        acquisition_stage_cls=BoundedAcquisitionStage,
        extraction_stage_cls=ProductionBoundedExtractionStage,
    )


def build_orchestrator(
    config: StoreConfig | None = None,
    *,
    orchestrator_config=None,
):
    """Build a fully wired production ``ResearchOrchestrator``."""
    config = config or StoreConfig.from_env()
    if orchestrator_config is None:
        governor = build_resource_governor(config)
        orchestrator_config = OrchestratorConfig(resource_governor=governor)
    elif getattr(orchestrator_config, "resource_governor", None) is None:
        governor = build_resource_governor(config)
        orchestrator_config = OrchestratorConfig(
            execution_mode=getattr(
                orchestrator_config, "execution_mode", "autonomous_local"
            ),
            budget_policy_version=getattr(
                orchestrator_config, "budget_policy_version", "budget-policy-v1"
            ),
            max_adaptive_cycles=getattr(orchestrator_config, "max_adaptive_cycles", 10),
            resume_on_conflict=getattr(orchestrator_config, "resume_on_conflict", True),
            resource_governor=governor,
            host_artifact_supplier=getattr(
                orchestrator_config, "host_artifact_supplier", None
            ),
        )
    return build_production_orchestrator(
        config, orchestrator_config=orchestrator_config
    )


__all__ = [
    "ProductionBoundedExtractionStage",
    "UowFactory",
    "build_acquisition_service",
    "build_audit_service",
    "build_claim_service",
    "build_direct_scrape_service",
    "build_evidence_service",
    "build_extraction_service",
    "build_invocation_service",
    "build_orchestrator",
    "build_production_orchestrator",
    "build_production_resumable_orchestrator",
    "build_resource_governor",
    "build_run_service",
    "build_semantic_service",
    "build_service",
    "build_strategy_service",
    "build_uow_factory",
    "build_workflow_operation_service",
]
