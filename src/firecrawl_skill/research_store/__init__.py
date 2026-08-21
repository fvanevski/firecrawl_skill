"""Persistent research asset store for the Firecrawl skill."""

from . import bounded_orchestrator as _bounded_orchestrator
from . import corpus_service as _corpus_service
from . import postgres as _postgres
from . import run_service as _run_service
from . import workflow_service as _workflow_service
from .acquisition.adapters.firecrawl_scrape import FirecrawlDirectScrapeAdapter
from .acquisition.authority import (
    AcquisitionPreflightError,
    AuthoritativeAcquisitionContext,
    require_authoritative_acquisition,
)
from .acquisition.direct_scrape_application import (
    DirectScrapeError,
    DirectScrapePersistenceError,
    DirectScrapeService,
)
from .acquisition.models import (
    DirectScrapeBatchResult,
    DirectScrapeItemResult,
    DirectScrapeRequest,
    ScrapeTransportResult,
)
from .acquisition.service import AcquisitionService
from .assessment.quality import QualityEvaluationError, QualityService
from .blob import ContentAddressedBlobStore
from .checkpoint_orchestrator import CheckpointResearchOrchestrator
from .composition import build_direct_scrape_service
from .config import StoreConfig
from .corpus_service import CorpusService
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
from .semantic_service import SemanticCallService
from .stages import ContextKeys, StageHandler, StageOutcome, StageResult

# Preserve the public import path while ensuring every newly constructed run
# service uses the terminal-decision guard. The checkpoint orchestrator selects
# its durable indexing stage only when the run service advertises that
# capability, leaving the base orchestrator independently reusable and testable.
# Wrapper checkpoint wiring remains explicit in the composition root.
_run_service.ResearchRunService = GuardedResearchRunService
ResearchRunService = GuardedResearchRunService
_workflow_service.ResearchRunService = GuardedResearchRunService
ResearchOrchestrator = CheckpointResearchOrchestrator

# Issues #255-#259 establish explicit repository objects on the canonical UoW.
# The final topology installs no generic domain-method aliases and does not use
# uow.runs as a cross-domain router. Only the separately documented class APIs
# below remain as behavioral compatibility exceptions.
install_shared_repository_context(_postgres)


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
    """Preserve the campaign-required class signature outside postgres.py."""
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

# Issue #217 remains an explicit campaign-required compatibility contract on the
# UoW class. Its authoritative SQL implementation is also consumed by
# PostgresCorpusRepository through a private connection-only adapter. These six
# class methods remain directly callable for the published #217 behavioral API;
# they are not generic repository-routing delegates and no entered-UoW instance
# aliases are installed for unrelated domain operations.
install_issue_217_contract(_postgres, _corpus_service, _bounded_orchestrator)
_postgres.PostgresUnitOfWork._has_sealed_at_column = staticmethod(_has_sealed_at_column)
_postgres.PostgresUnitOfWork._has_extraction_attempt_id_column = staticmethod(
    _has_extraction_attempt_id_column
)

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
