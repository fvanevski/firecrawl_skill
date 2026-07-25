"""Persistent research asset store for the Firecrawl skill."""

from .acquisition_service import AcquisitionService, FirecrawlSearchAdapter
from .compat_export import CompatibilityExportResult, SearchCompatibilityExporter
from .config import StoreConfig
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
from .legacy_adapter import AdapterMode, LegacyEntryPointAdapter
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
    "AcquisitionService",
    "AdapterMode",
    "BlobReference",
    "CompatibilityExportResult",
    "ContextKeys",
    "CorpusService",
    "ExecutionModePolicy",
    "ExtractionAttempt",
    "ExtractionAttemptRepository",
    "ExtractionError",
    "ExtractionQualityMetrics",
    "ExtractionService",
    "FirecrawlSearchAdapter",
    "LegacyEntryPointAdapter",
    "NormalizedBlock",
    "OrchestratorConfig",
    "OrchestratorResult",
    "QualityConfig",
    "QualityEvaluationError",
    "QualityService",
    "ResearchOrchestrator",
    "ResearchRunService",
    "SearchCompatibilityExporter",
    "SemanticCallService",
    "StageHandler",
    "StageOutcome",
    "StageResult",
    "StoreConfig",
    "TransformationRecord",
    "evaluate_quality",
]
