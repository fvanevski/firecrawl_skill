"""Persistent research asset store for the Firecrawl skill."""

from . import orchestrator as _orchestrator
from . import run_service as _run_service
from . import workflow_service as _workflow_service
from .acquisition_authority import (
    AcquisitionPreflightError,
    AuthoritativeAcquisitionContext,
    require_authoritative_acquisition,
)
from .acquisition_service import AcquisitionService, FirecrawlSearchAdapter
from .checkpoint_indexing_stage import CheckpointIndexingStage
from .checkpoint_orchestrator import CheckpointResearchOrchestrator
from .checkpoint_workflow_service import CheckpointWorkflowOperationService
from .config import StoreConfig
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
from .lifecycle_guard import GuardedResearchRunService
from .orchestrator import OrchestratorConfig, OrchestratorResult
from .quality_config import QualityConfig
from .quality_evaluator import evaluate_quality
from .quality_service import QualityEvaluationError, QualityService
from .semantic_service import SemanticCallService
from .service import CorpusService
from .stages import ContextKeys, StageHandler, StageOutcome, StageResult

# Preserve the public import path while ensuring every newly constructed run
# service uses the terminal-decision guard.  Assigning the submodule attribute
# also covers ``from research_store.run_service import ResearchRunService``.
_run_service.ResearchRunService = GuardedResearchRunService
ResearchRunService = GuardedResearchRunService
_orchestrator.ResearchRunService = GuardedResearchRunService
_workflow_service.ResearchRunService = GuardedResearchRunService
_workflow_service.WorkflowOperationService = CheckpointWorkflowOperationService
_orchestrator.IndexingStage = CheckpointIndexingStage
_orchestrator.ResearchOrchestrator = CheckpointResearchOrchestrator
ResearchOrchestrator = CheckpointResearchOrchestrator

__all__ = [
    "VALID_NORMALIZATION_DISPOSITIONS",
    "VALID_NORMALIZATION_RULE_IDS",
    "AcquisitionPreflightError",
    "AcquisitionService",
    "AuthoritativeAcquisitionContext",
    "BlobReference",
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
