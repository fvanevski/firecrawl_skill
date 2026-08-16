"""Persistent research asset store for the Firecrawl skill."""

from . import acquisition_service as _acquisition_service
from . import bounded_orchestrator as _bounded_orchestrator
from . import candidate_policy_service as _candidate_policy_service
from . import corpus_service as _corpus_service
from . import orchestrator as _orchestrator
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
from .bounded_orchestrator import BoundedAcquisitionStage, BoundedExtractionStage
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
from .postgres_acquisition import install_candidate_policy_repository
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
_orchestrator.ResearchRunService = GuardedResearchRunService
_workflow_service.ResearchRunService = GuardedResearchRunService
_orchestrator.ResearchOrchestrator = CheckpointResearchOrchestrator
ResearchOrchestrator = CheckpointResearchOrchestrator

# Issue #216 canonical provider/stage routing. ``AcquisitionService`` resolves
# its default adapter from the module global at construction time, and
# ``ResearchOrchestrator.__init__`` resolves its stage classes the same way.
# Rebinding those established extension points keeps every public builder,
# checkpoint orchestrator, and smart-resume subclass on the bounded production
# seam without duplicating lifecycle or transaction machinery.
_acquisition_service.FirecrawlSearchAdapter = BoundedFirecrawlSearchAdapter
FirecrawlSearchAdapter = BoundedFirecrawlSearchAdapter
_orchestrator.AcquisitionStage = BoundedAcquisitionStage
_orchestrator.ExtractionStage = BoundedExtractionStage

# Issue #255 establishes explicit repository objects on the canonical UoW while
# retaining the existing PostgreSQL domain methods as temporary delegates. The
# later Phase-3 repository-extraction issues replace those delegates without
# changing the one-connection transaction boundary established here.
install_shared_repository_context(_postgres)

# Issue #258 routes the established CandidatePolicyService ranking-decision
# compatibility surface through the canonical candidate repository. The policy
# service keeps policy evaluation; PostgreSQL writes have one repository owner.
install_candidate_policy_repository(_candidate_policy_service)

# Issue #217 installs the authoritative batch timing/outcome/selection contract
# on the canonical corpus production extension point. The installer mutates the
# already-imported class in place so existing compatibility references held by
# builders, tests, and checkpoint wrappers receive the exact same PostgreSQL
# behavior.
install_issue_217_contract(_postgres, _corpus_service, _bounded_orchestrator)

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
