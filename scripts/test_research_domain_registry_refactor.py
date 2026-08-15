from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from research_domain import models as legacy_models
from research_domain._catalog import CANONICAL_MODELS, _schema_owners
from research_domain.codec import schema_for
from research_domain.registry import (
    COMPATIBILITY_POLICY,
    CURRENT_VERSION_BY_MODEL,
    MODEL_BY_VERSION,
    load_model,
    schema_registry,
    serialize_model,
)

FIXTURES = ROOT / "tests" / "fixtures" / "research_domain"
SCHEMAS = ROOT / "schemas" / "research-workflow"
VALID = json.loads((FIXTURES / "valid.json").read_text())

EXPECTED_MODEL_BY_VERSION = {
    "research-spec-v1": "ResearchSpec",
    "search-plan-v1": "SearchPlan",
    "candidate-assessment-v1": "CandidateAssessment",
    "coverage-ledger-v1": "CoverageLedger",
    "strategy-revision-v1": "StrategyRevisionProposal",
    "evidence-packet-v1": "EvidencePacket",
    "terminal-decision-v1": "TerminalDecision",
    "handoff-payload-v1": "HandoffPayload",
    "benchmark-dataset-v2": "BenchmarkDataset",
    "benchmark-objective-v2": "BenchmarkObjective",
    "benchmark-source-v2": "BenchmarkSource",
    "quality-measurement-v3": "QualityMeasurement",
    "performance-measurement-v2": "PerformanceMeasurement",
    "integrity-check-v1": "DeterministicIntegrityCheck",
    "workflow-run-result-v1": "WorkflowRunResult",
    "workflow-comparison-v1": "WorkflowComparison",
    "release-recommendation-v1": "ReleaseRecommendation",
    "token-accounting-v1": "TokenAccounting",
    "cache-event-v1": "CacheEvent",
    "embedding-throughput-v1": "EmbeddingThroughputRecord",
    "resource-sample-v1": "ResourceSample",
    "endpoint-usage-v1": "EndpointUsageRecord",
    "performance-telemetry-summary-v1": "PerformanceTelemetrySummary",
    "quality-measurement-v1": "QualityMeasurement",
    "quality-measurement-v2": "QualityMeasurement",
    "performance-measurement-v1": "PerformanceMeasurement",
}

EXPECTED_CURRENT_VERSION_BY_MODEL = {
    "ResearchSpec": "research-spec-v1",
    "SearchPlan": "search-plan-v1",
    "CandidateAssessment": "candidate-assessment-v1",
    "CoverageLedger": "coverage-ledger-v1",
    "StrategyRevisionProposal": "strategy-revision-v1",
    "EvidencePacket": "evidence-packet-v1",
    "TerminalDecision": "terminal-decision-v1",
    "HandoffPayload": "handoff-payload-v1",
    "BenchmarkDataset": "benchmark-dataset-v2",
    "BenchmarkObjective": "benchmark-objective-v2",
    "BenchmarkSource": "benchmark-source-v2",
    "QualityMeasurement": "quality-measurement-v3",
    "PerformanceMeasurement": "performance-measurement-v2",
    "DeterministicIntegrityCheck": "integrity-check-v1",
    "WorkflowRunResult": "workflow-run-result-v1",
    "WorkflowComparison": "workflow-comparison-v1",
    "ReleaseRecommendation": "release-recommendation-v1",
    "TokenAccounting": "token-accounting-v1",
    "CacheEvent": "cache-event-v1",
    "EmbeddingThroughputRecord": "embedding-throughput-v1",
    "ResourceSample": "resource-sample-v1",
    "EndpointUsageRecord": "endpoint-usage-v1",
    "PerformanceTelemetrySummary": "performance-telemetry-summary-v1",
}

EXPECTED_CAPABILITY_MODULE_BY_MODEL = {
    "ResearchSpec": "research_domain.research",
    "SearchPlan": "research_domain.research",
    "CandidateAssessment": "research_domain.acquisition",
    "CoverageLedger": "research_domain.research",
    "StrategyRevisionProposal": "research_domain.research",
    "EvidencePacket": "research_domain.assessment",
    "TerminalDecision": "research_domain.research",
    "HandoffPayload": "research_domain.reporting",
    "BenchmarkDataset": "research_domain.release",
    "BenchmarkObjective": "research_domain.release",
    "BenchmarkSource": "research_domain.release",
    "QualityMeasurement": "research_domain.release",
    "PerformanceMeasurement": "research_domain.release",
    "DeterministicIntegrityCheck": "research_domain.release",
    "WorkflowRunResult": "research_domain.release",
    "WorkflowComparison": "research_domain.release",
    "ReleaseRecommendation": "research_domain.release",
    "TokenAccounting": "research_domain.telemetry",
    "CacheEvent": "research_domain.telemetry",
    "EmbeddingThroughputRecord": "research_domain.telemetry",
    "ResourceSample": "research_domain.telemetry",
    "EndpointUsageRecord": "research_domain.telemetry",
    "PerformanceTelemetrySummary": "research_domain.telemetry",
}


def test_complete_schema_registry_matches_pre_refactor_contract():
    assert {
        version: model.__name__ for version, model in MODEL_BY_VERSION.items()
    } == EXPECTED_MODEL_BY_VERSION
    assert CURRENT_VERSION_BY_MODEL == EXPECTED_CURRENT_VERSION_BY_MODEL
    assert set(VALID) == set(EXPECTED_MODEL_BY_VERSION)
    assert set(schema_registry()) == set(EXPECTED_MODEL_BY_VERSION)


@pytest.mark.parametrize("version", EXPECTED_MODEL_BY_VERSION)
def test_registered_serialization_and_schema_semantics_are_preserved(version):
    model_type = MODEL_BY_VERSION[version]
    payload = VALID[version]

    assert serialize_model(load_model(payload)) == payload
    assert schema_registry()[version] == schema_for(model_type)

    current_version = EXPECTED_CURRENT_VERSION_BY_MODEL[model_type.__name__]
    if version == current_version:
        assert json.loads((SCHEMAS / f"{version}.json").read_text()) == schema_for(
            model_type
        )
        assert COMPATIBILITY_POLICY[version] == {
            "current": True,
            "readable_versions": (version,),
            "write_version": version,
            "predecessors": (),
        }
    else:
        assert COMPATIBILITY_POLICY[version] == {
            "current": False,
            "readable_versions": (version, current_version),
            "write_version": current_version,
            "predecessors": (),
        }


def test_canonical_models_live_in_capability_modules_and_legacy_facade_reexports():
    assert len(CANONICAL_MODELS) == len(EXPECTED_CURRENT_VERSION_BY_MODEL)
    for model in CANONICAL_MODELS:
        assert model.__module__ == EXPECTED_CAPABILITY_MODULE_BY_MODEL[model.__name__]
        assert getattr(legacy_models, model.__name__) is model


def test_schema_owner_validation_rejects_duplicate_canonical_registration():
    class Duplicate:
        SCHEMA_VERSION = "duplicate-v1"

    with pytest.raises(RuntimeError, match="duplicate canonical model registration"):
        _schema_owners((Duplicate, Duplicate))


def test_schema_owner_validation_rejects_cross_model_version_collision():
    class First:
        SCHEMA_VERSION = "collision-v1"

    class Second:
        SCHEMA_VERSION = "collision-v1"

    with pytest.raises(RuntimeError, match="schema version has multiple"):
        _schema_owners((First, Second))


def test_legacy_registry_schema_generation_remains_bound_to_current_model_schema():
    assert (
        schema_registry()["quality-measurement-v1"]
        == schema_registry()["quality-measurement-v3"]
    )
    assert (
        schema_registry()["quality-measurement-v2"]
        == schema_registry()["quality-measurement-v3"]
    )
    assert (
        schema_registry()["performance-measurement-v1"]
        == schema_registry()["performance-measurement-v2"]
    )
