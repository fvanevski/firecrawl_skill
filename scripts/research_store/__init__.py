"""Persistent research asset store for the Firecrawl skill."""

from . import acquisition_service as _acquisition_service
from . import bounded_orchestrator as _bounded_orchestrator
from . import corpus_service as _corpus_service
from . import postgres as _postgres
from . import run_service as _run_service
from . import workflow_service as _workflow_service
from .acquisition_authority import (
    AcquisitionPreflightError,
    AuthoritativeAcquisitionContext,
    require_authoritative_acquisition,
)
from .acquisition_service import AcquisitionService
from .blob import ContentAddressedBlobStore
from .bounded_acquisition import BoundedFirecrawlSearchAdapter
from .checkpoint_orchestrator import CheckpointResearchOrchestrator
from .config import StoreConfig
from .corpus_service import CorpusService
from .direct_scrape_service import (
    DirectScrapeBatchResult,
    DirectScrapeError,
    DirectScrapeItemResult,
    DirectScrapePersistenceError,
    DirectScrapeRequest,
    DirectScrapeService,
    FirecrawlDirectScrapeAdapter,
    ScrapeTransportResult,
    build_direct_scrape_service,
)
from .domain import (
    VALID_NORMALIZATION_DISPOSITIONS,
    VALID_NORMALIZATION_RULE_IDS,
    BlobReference,
    ExtractionAttempt,
    ExtractionQualityMetrics,
    NormalizedBlock,
    TransformationRecord,
)
from .execution_policy import ExecutionModePolicy
from .extraction_repository import ExtractionAttemptRepository
from .extraction_service import ExtractionError, ExtractionService
from .ingestion_batch_semantics import install_issue_217_contract
from .lifecycle_guard import GuardedResearchRunService
from .orchestrator import OrchestratorConfig, OrchestratorResult
from .postgres_batch_schema import (
    _has_extraction_attempt_id_column,
    _has_sealed_at_column,
)
from .postgres_uow_core import install_shared_repository_context
from .provider_preflight import (
    CandidatePreflightChecker,
    CandidatePreflightResult,
    ExtractionDeadlinePolicy,
)
from .quality_config import QualityConfig
from .quality_evaluator import evaluate_quality
from .quality_service import QualityEvaluationError, QualityService
from .semantic_service import SemanticCallService
from .stages import ContextKeys, StageHandler, StageOutcome, StageResult

# Preserve the public import path while ensuring every newly constructed run
# service uses the terminal-decision guard. The checkpoint orchestrator selects
# its durable indexing stage only when the run service advertises that
# capability, leaving the base orchestrator independently reusable and testable.
# Wrapper checkpoint wiring remains explicit in container.py.
_run_service.ResearchRunService = GuardedResearchRunService
ResearchRunService = GuardedResearchRunService
_workflow_service.ResearchRunService = GuardedResearchRunService
ResearchOrchestrator = CheckpointResearchOrchestrator

# Issue #216 canonical provider/stage routing. ``AcquisitionService`` resolves
# its default adapter from the module global at construction time.
# Stage class injection is handled explicitly by the composition root
# (``orchestration.composition.build_production_orchestrator``).
_acquisition_service.FirecrawlSearchAdapter = BoundedFirecrawlSearchAdapter
FirecrawlSearchAdapter = BoundedFirecrawlSearchAdapter

# Issue #255 establishes explicit repository objects on the canonical UoW while
# retaining temporary compatibility delegates. Issue #259 completes the
# extraction so those delegates contain no domain SQL of their own.
install_shared_repository_context(_postgres)


# Direct-scrape callers still inspect the historical class-level persist_ingest
# signature to verify that parser_name remains an additive trailing parameter.
# Keep that campaign-required compatibility surface outside postgres.py. An
# entered UoW shadows this facade with the canonical PostgresCorpusRepository
# bound method installed by postgres_uow_core.py, so persistence ownership does
# not return to the UoW.
def _persist_ingest_compatibility_facade(
    self,
    request,
    canonical_url,
    blob,
    normalized_text,
    blocks,
    chunks,
    parser_version,
    chunker_version,
    normalization_version,
    chunker_name="structural",
    parser_name="markdown",
):
    repository = getattr(self, "snapshots", None)
    if repository is None:
        raise RuntimeError("PostgresUnitOfWork must be entered before persist_ingest")
    return repository.persist_ingest(
        request,
        canonical_url,
        blob,
        normalized_text,
        blocks,
        chunks,
        parser_version,
        chunker_version,
        normalization_version,
        chunker_name=chunker_name,
        parser_name=parser_name,
    )


_postgres.PostgresUnitOfWork.persist_ingest = _persist_ingest_compatibility_facade

# Issue #258 keeps candidate ranking policy and persistence routing explicit in
# CandidatePolicyService.record_rankings(). The service delegates directly to
# the canonical candidate repository, so no import-time method replacement is
# required and source-level navigation matches runtime behavior.

# Issue #217 remains a campaign-required compatibility facade on the UoW class.
# Its authoritative SQL implementation is also consumed by
# PostgresCorpusRepository through a connection-only adapter. Once a UoW is
# entered, issue #259 installs repository-bound instance delegates that override
# the class facade without changing #217's timing/outcome/selection contract.
install_issue_217_contract(_postgres, _corpus_service, _bounded_orchestrator)
# The #217 class facade has two private schema-probe dependencies that legacy
# tests invoke without entering a UoW. Keep their SQL outside postgres.py while
# retaining that temporary compatibility shape.
_postgres.PostgresUnitOfWork._has_sealed_at_column = staticmethod(_has_sealed_at_column)
_postgres.PostgresUnitOfWork._has_extraction_attempt_id_column = staticmethod(
    _has_extraction_attempt_id_column
)

# ARC-17 correction is now baked into CorpusService.bounded_ingest_batch and
# ExtractionService.complete_attempt idempotency guard. No monkeypatching is
# required; the bounded stage calls the explicit production path directly.

__all__ = [
    "VALID_NORMALIZATION_DISPOSITIONS",
    "VALID_NORMALIZATION_RULE_IDS",
    "AcquisitionPreflightError",
    "AcquisitionService",
    "AuthoritativeAcquisitionContext",
    "BlobReference",
    "CandidatePreflightChecker",
    "CandidatePreflightResult",
    "ContentAddressedBlobStore",
    "ContextKeys",
    "CorpusService",
    "DirectScrapeBatchResult",
    "DirectScrapeError",
    "DirectScrapeItemResult",
    "DirectScrapePersistenceError",
    "DirectScrapeRequest",
    "DirectScrapeService",
    "ExecutionModePolicy",
    "ExtractionAttempt",
    "ExtractionAttemptRepository",
    "ExtractionDeadlinePolicy",
    "ExtractionError",
    "ExtractionQualityMetrics",
    "ExtractionService",
    "FirecrawlDirectScrapeAdapter",
    "FirecrawlSearchAdapter",
    "NormalizedBlock",
    "OrchestratorConfig",
    "OrchestratorResult",
    "QualityConfig",
    "QualityEvaluationError",
    "QualityService",
    "ResearchOrchestrator",
    "ResearchRunService",
    "ScrapeTransportResult",
    "SemanticCallService",
    "StageHandler",
    "StageOutcome",
    "StageResult",
    "StoreConfig",
    "TransformationRecord",
    "build_direct_scrape_service",
    "evaluate_quality",
    "require_authoritative_acquisition",
]
