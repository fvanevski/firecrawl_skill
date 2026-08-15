"""Canonical research-domain model registration catalog."""

from __future__ import annotations

from . import acquisition, assessment, release, reporting, research, telemetry

CANONICAL_MODELS = (
    research.ResearchSpec,
    research.SearchPlan,
    acquisition.CandidateAssessment,
    research.CoverageLedger,
    research.StrategyRevisionProposal,
    assessment.EvidencePacket,
    research.TerminalDecision,
    reporting.HandoffPayload,
    # Phase 7, issue #67 — Release benchmark campaign
    release.BenchmarkDataset,
    release.BenchmarkObjective,
    release.BenchmarkSource,
    release.QualityMeasurement,
    release.PerformanceMeasurement,
    release.DeterministicIntegrityCheck,
    release.WorkflowRunResult,
    release.WorkflowComparison,
    release.ReleaseRecommendation,
    # Phase 7, issue #143 — Run-scoped performance telemetry
    telemetry.TokenAccounting,
    telemetry.CacheEvent,
    telemetry.EmbeddingThroughputRecord,
    telemetry.ResourceSample,
    telemetry.EndpointUsageRecord,
    telemetry.PerformanceTelemetrySummary,
)


def _schema_owners(models: tuple[type, ...]) -> dict[str, type]:
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
