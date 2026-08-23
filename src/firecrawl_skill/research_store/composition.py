"""Canonical production composition root for the research store.

This module is the canonical ``StoreConfig``-driven surface that constructs
unit-of-work factories, infrastructure adapters, application services, and
production orchestrators. It owns wiring only: deterministic policy,
persistence semantics, workflow decisions, and transaction behavior remain in
their respective services and repositories.

``production_topology`` is a deliberately smaller leaf wiring primitive used by
this root when production bounded extraction is required. It is not a second
service/UoW composition root.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from .acquisition.adapters.bounded_firecrawl import BoundedFirecrawlSearchAdapter
from .acquisition.authority import require_authoritative_acquisition
from .acquisition.service import AcquisitionService
from .blob import ContentAddressedBlobStore
from .bounded_orchestrator import BoundedAcquisitionStage
from .budget_policy import DEFAULT_POLICY
from .checkpoint_orchestrator import CheckpointResearchOrchestrator
from .config import StoreConfig
from .corpus_service import CorpusService
from .coverage_seed_service import CompleteCoverageService
from .extraction_service import ExtractionService
from .lifecycle_guard import GuardedResearchRunService as ResearchRunService
from .orchestrator import OrchestratorConfig, ResearchOrchestrator
from .postgres import PostgresUnitOfWork
from .production_topology import ProductionBoundedExtractionStage
from .retrieval.projection.indexing import OpenAICompatibleEmbedder
from .retrieval.projection.qdrant import QdrantIndex
from .retrieval.ranking import CohereCompatibleReranker
from .semantic_service import SemanticCallService
from .strategy_service import StrategyRevisionService
from .terminal_decision_service import TerminalDecisionService
from .valkey_queue import ValkeyQueue

if TYPE_CHECKING:
    from .acquisition.direct_scrape_application import DirectScrapeService
    from .acquisition.ports import DirectScrapeAdapter

logger = logging.getLogger(__name__)

UowFactory = Callable[[], PostgresUnitOfWork]


def build_uow_factory(config: StoreConfig) -> UowFactory:
    """Bind the canonical PostgreSQL unit-of-work constructor to ``config``.

    Deliberately does not call ``require_database``: historical low-level
    factory surfaces only bound constructor arguments. Public service builders
    retain their existing fail-fast database validation before composition.
    Issue-300 temporal repository strengthening is installed inside the shared
    canonical repository context rather than by substituting a second UoW type.
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
    from .assessment.audit import AuditService

    return ResearchRunService(
        build_uow_factory(config),
        blob_store=ContentAddressedBlobStore(config.blob_root),
        audit_service_factory=AuditService,
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
    return SemanticCallService(
        build_uow_factory(config),
        host_artifact_supplier=config.host_artifact_supplier,
    )


def build_acquisition_service(
    config: StoreConfig | None = None, search_adapter=None
) -> Any:
    """Compose acquisition policy with exact local recency semantics."""
    from .acquisition.temporal_acquisition import TemporalAcquisitionService

    config = config or StoreConfig.from_env()
    config.require_database()
    adapter = (
        search_adapter
        if search_adapter is not None
        else BoundedFirecrawlSearchAdapter()
    )
    base = AcquisitionService(
        build_uow_factory(config),
        blob_store=ContentAddressedBlobStore(config.blob_root),
        search_adapter=adapter,
        config=config,
        authority_preflight=require_authoritative_acquisition,
    )
    return TemporalAcquisitionService(base)


def build_strategy_service(
    config: StoreConfig | None = None,
) -> StrategyRevisionService:
    config = config or StoreConfig.from_env()
    config.require_database()
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
    from .assessment.evidence import EvidenceService

    config = config or StoreConfig.from_env()
    config.require_database()
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
    """Compose direct scrape with canonical candidate temporal provenance."""
    from .acquisition.replay_safe_direct_scrape import ReplaySafeDirectScrapeService
    from .parsing import get_registry
    from .temporal_corpus import TemporalCorpusService

    if adapter_factory is None:
        from .acquisition.adapters.firecrawl_scrape import FirecrawlDirectScrapeAdapter

        adapter_factory = FirecrawlDirectScrapeAdapter

    resolved = config or StoreConfig.from_env()
    resolved.require_database()
    uow_factory = build_uow_factory(resolved)
    blob_store = ContentAddressedBlobStore(resolved.blob_root)
    corpus_service = TemporalCorpusService(
        CorpusService(
            resolved,
            uow_factory,
            blob_store,
            parser_registry=get_registry(),
        ),
        uow_factory,
    )
    return ReplaySafeDirectScrapeService(
        resolved,
        uow_factory,
        blob_store,
        corpus_service,
        adapter_factory=adapter_factory,
    )


def build_fscrape_service(
    config: StoreConfig | None = None,
    *,
    adapter_factory: Callable[[], Any] | None = None,
):
    """Build the validated authoritative ``fscrape`` application service."""
    from .acquisition.adapters.firecrawl_scrape import FirecrawlDirectScrapeAdapter
    from .fscrape_authority import (
        CanonicalFScrapeService,
        ReplaySafeValidatedDirectScrapeService,
    )

    resolved = config or StoreConfig.from_env()
    resolved.require_database()
    selected_factory = adapter_factory or FirecrawlDirectScrapeAdapter
    base = build_direct_scrape_service(resolved, adapter_factory=selected_factory)
    direct = ReplaySafeValidatedDirectScrapeService(
        base.config,
        base.uow_factory,
        base.blob_store,
        base.corpus_service,
        adapter_factory=base.adapter_factory,
        preflight=base.preflight,
        authority_check=base.authority_check,
        queue=base.queue,
        preflight_checker=base.preflight_checker,
        budget=base.budget,
    )
    return CanonicalFScrapeService(direct, build_run_service(resolved))


def build_policy_fsearch_service(
    config: StoreConfig | None = None,
    *,
    search_adapter_factory: Callable[[], Any] | None = None,
):
    """Build the policy-complete authoritative ``fsearch`` application service."""
    from .acquisition.adapters.firecrawl_search import (
        MetadataOnlyFirecrawlSearchAdapter,
    )
    from .candidate_policy_service import CandidatePolicyService
    from .temporal_fsearch_policy import TemporalPolicyFSearchService

    resolved = config or StoreConfig.from_env()
    resolved.require_database()
    selected_factory = search_adapter_factory or MetadataOnlyFirecrawlSearchAdapter
    run_service = build_run_service(resolved)
    return TemporalPolicyFSearchService(
        resolved,
        run_service,
        build_invocation_service(resolved),
        acquisition_factory=lambda: build_acquisition_service(
            resolved, search_adapter=selected_factory()
        ),
        direct_scrape_factory=lambda: build_direct_scrape_service(resolved),
        policy_service=CandidatePolicyService(run_service.uow_factory),
    )


def build_fsearch_service(
    config: StoreConfig | None = None,
    *,
    search_adapter_factory: Callable[[], Any] | None = None,
):
    """Canonical public alias for policy-complete ``fsearch`` construction."""
    return build_policy_fsearch_service(
        config,
        search_adapter_factory=search_adapter_factory,
    )


def build_inspection_service(config: StoreConfig | None = None):
    """Build database-native inspection with authoritative scrape injection."""
    from .inspection_service import InspectionService

    resolved = config or StoreConfig.from_env()
    resolved.require_database()
    return InspectionService(
        resolved,
        direct_scrape_factory=lambda: build_direct_scrape_service(resolved),
    )


def build_orchestrator_instance(
    orchestrator_cls: type[ResearchOrchestrator],
    config: StoreConfig | None = None,
    *,
    orchestrator_config: OrchestratorConfig | None = None,
    corpus_service: Any | None = None,
    terminal_config: Any | None = None,
    acquisition_stage_cls: Any | None = None,
    extraction_stage_cls: Any | None = None,
    indexing_stage_cls: Any | None = None,
) -> ResearchOrchestrator:
    """Wire an orchestrator class without making application code a root."""
    from .terminal_decision import TerminalDecisionConfig

    resolved = config or StoreConfig.from_env()
    resolved.require_database()
    resolved_orchestrator_config = orchestrator_config or OrchestratorConfig()
    run_service = build_run_service(resolved)
    acquisition_service = build_acquisition_service(resolved)
    strategy_service = build_strategy_service(resolved)
    coverage_service = CompleteCoverageService(run_service.uow_factory)

    resolved_corpus = corpus_service
    if resolved_corpus is None:
        try:
            resolved_corpus = build_service(resolved)
        except Exception as exc:  # noqa: BLE001
            logger.debug("corpus_service auto-build deferred: %s", exc)
            resolved_corpus = None

    extraction_service = None
    try:
        extraction_service = build_extraction_service(resolved)
    except Exception as exc:  # noqa: BLE001
        logger.debug("extraction_service auto-build deferred: %s", exc)
        extraction_service = None

    resolved_terminal_config = terminal_config or TerminalDecisionConfig.load()
    terminal_service = TerminalDecisionService(run_service.uow_factory)
    evidence_service = build_evidence_service(resolved)

    return orchestrator_cls(
        run_service=run_service,
        coverage_service=coverage_service,
        strategy_service=strategy_service,
        acquisition_service=acquisition_service,
        config=resolved,
        corpus_service=resolved_corpus,
        terminal_config=resolved_terminal_config,
        terminal_service=terminal_service,
        orchestrator_config=resolved_orchestrator_config,
        extraction_service=extraction_service,
        evidence_service=evidence_service,
        acquisition_stage_cls=acquisition_stage_cls,
        extraction_stage_cls=extraction_stage_cls,
        indexing_stage_cls=indexing_stage_cls,
    )


def build_production_orchestrator(
    config: StoreConfig,
    *,
    orchestrator_config: OrchestratorConfig | None = None,
) -> ResearchOrchestrator:
    """Build the fresh production orchestrator with bounded stages."""
    return build_orchestrator_instance(
        CheckpointResearchOrchestrator,
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

    return build_orchestrator_instance(
        ProvenanceResumableResearchOrchestrator,
        config,
        orchestrator_config=orchestrator_config,
        acquisition_stage_cls=BoundedAcquisitionStage,
        extraction_stage_cls=ProductionBoundedExtractionStage,
    )


def build_curated_synthesis_service(config: StoreConfig | None = None):
    """Build the supported post-seal evidence + synthesis application service."""
    from .asset_promotion_service import AssetPromotionService
    from .curated_synthesis_service import (
        AuthorityAlignedLocalSynthesisService,
        CuratedSynthesisService,
    )
    from .evidence_preparation_service import EvidencePreparationService

    resolved = config or StoreConfig.from_env()
    resolved.require_database()
    run_service = build_run_service(resolved)
    coverage_service = CompleteCoverageService(run_service.uow_factory)
    evidence_service = build_evidence_service(resolved)
    semantic_service = build_semantic_service(resolved)
    preparation = EvidencePreparationService(
        corpus_service=build_service(resolved),
        evidence_service=evidence_service,
        coverage_service=coverage_service,
        semantic_service=semantic_service,
        config=resolved,
    )
    synthesis = AuthorityAlignedLocalSynthesisService(
        semantic_service,
        evidence_service,
        resolved,
        resource_governor=build_resource_governor(resolved),
    )
    return CuratedSynthesisService(
        config=resolved,
        run_service=run_service,
        promotion_service=AssetPromotionService(run_service.uow_factory),
        evidence_preparation_service=preparation,
        synthesis_service=synthesis,
        coverage_service=coverage_service,
    )


def build_curated_run_service(config: StoreConfig | None = None):
    """Build the operator-facing curated run service from canonical roots."""
    from .asset_promotion_service import AssetPromotionService
    from .curated_run_service import CuratedRunService

    resolved = config or StoreConfig.from_env()
    resolved.require_database()
    run_service = build_run_service(resolved)
    return CuratedRunService(
        run_service,
        build_workflow_operation_service(resolved),
        AssetPromotionService(run_service.uow_factory),
        synthesis_service=lambda: build_curated_synthesis_service(resolved),
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
    "build_curated_run_service",
    "build_curated_synthesis_service",
    "build_direct_scrape_service",
    "build_evidence_service",
    "build_extraction_service",
    "build_fscrape_service",
    "build_fsearch_service",
    "build_inspection_service",
    "build_invocation_service",
    "build_orchestrator",
    "build_orchestrator_instance",
    "build_policy_fsearch_service",
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
