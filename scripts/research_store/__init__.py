"""Persistent research asset store for the Firecrawl skill."""

from .acquisition_authority import (
    AcquisitionPreflightError,
    AuthoritativeAcquisitionContext,
    require_authoritative_acquisition,
)
from .acquisition_service import AcquisitionService, FirecrawlSearchAdapter
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
from .orchestrator import (
    OrchestratorConfig,
    OrchestratorResult,
    ResearchOrchestrator,
)
from .quality_config import QualityConfig
from .quality_evaluator import evaluate_quality
from .quality_service import QualityEvaluationError, QualityService
from .run_service import ResearchRunService
from .semantic_service import SemanticCallService
from .service import CorpusService
from .stages import (
    ContextKeys,
    StageHandler,
    StageOutcome,
    StageResult,
)

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
