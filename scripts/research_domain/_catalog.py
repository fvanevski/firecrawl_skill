"""Canonical research-domain model registration catalog."""

from __future__ import annotations

from collections.abc import Iterable

from .acquisition import CandidateAssessment
from .assessment import EvidencePacket
from .release import (
    BenchmarkDataset,
    BenchmarkObjective,
    BenchmarkSource,
    DeterministicIntegrityCheck,
    PerformanceMeasurement,
    QualityMeasurement,
    ReleaseRecommendation,
    WorkflowComparison,
    WorkflowRunResult,
)
from .reporting import HandoffPayload
from .research import (
    CoverageLedger,
    ResearchSpec,
    SearchPlan,
    StrategyRevisionProposal,
    TerminalDecision,
)
from .telemetry import (
    CacheEvent,
    EmbeddingThroughputRecord,
    EndpointUsageRecord,
    PerformanceTelemetrySummary,
    ResourceSample,
    TokenAccounting,
)


CANONICAL_MODELS = (
    ResearchSpec,
    SearchPlan,
    CandidateAssessment,
    CoverageLedger,
    StrategyRevisionProposal,
    EvidencePacket,
    TerminalDecision,
    HandoffPayload,
    # Phase 7, issue #67 — Release benchmark campaign
    BenchmarkDataset,
    BenchmarkObjective,
    BenchmarkSource,
    QualityMeasurement,
    PerformanceMeasurement,
    DeterministicIntegrityCheck,
    WorkflowRunResult,
    WorkflowComparison,
    ReleaseRecommendation,
    # Phase 7, issue #143 — Run-scoped performance telemetry
    TokenAccounting,
    CacheEvent,
    EmbeddingThroughputRecord,
    ResourceSample,
    EndpointUsageRecord,
    PerformanceTelemetrySummary,
)


def _schema_owners(models: Iterable[type]) -> dict[str, type]:
    """Return the unique canonical owner for every readable schema version."""

    owners: dict[str, type] = {}
    names: dict[str, type] = {}
    seen_models: set[type] = set()

    for model in models:
        if model in seen_models:
            raise RuntimeError(f"duplicate canonical model registration: {model.__name__}")
        seen_models.add(model)

        existing_name = names.get(model.__name__)
        if existing_name is not None and existing_name is not model:
            raise RuntimeError(f"duplicate canonical model name: {model.__name__}")
        names[model.__name__] = model

        versions = (model.SCHEMA_VERSION, *getattr(model, "SCHEMA_VERSIONS", ()))
        for version in versions:
            existing = owners.get(version)
            if existing is not None and existing is not model:
                raise RuntimeError(
                    "schema version has multiple canonical model registrations: "
                    f"{version} -> {existing.__name__}, {model.__name__}"
                )
            owners[version] = model

    return owners


# Import-time validation prevents the registry's dict construction from silently
# collapsing a duplicate schema/version or model-name registration.
_SCHEMA_OWNERS = _schema_owners(CANONICAL_MODELS)
