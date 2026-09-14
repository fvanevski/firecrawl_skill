from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from firecrawl_skill.persisted_types import (
    COVERAGE_ITEM_TYPE,
    PersistedTypeRegistryError,
    PersistedTypeValue,
)
from firecrawl_skill.research_domain.research import CoverageItemType

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from firecrawl_skill.research_domain import models as legacy_models
from firecrawl_skill.research_domain._catalog import CANONICAL_MODELS, _schema_owners
from firecrawl_skill.research_domain.codec import schema_for
from firecrawl_skill.research_domain.registry import (
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
    "ResearchSpec": "firecrawl_skill.research_domain.research",
    "SearchPlan": "firecrawl_skill.research_domain.research",
    "CandidateAssessment": "firecrawl_skill.research_domain.acquisition",
    "CoverageLedger": "firecrawl_skill.research_domain.research",
    "StrategyRevisionProposal": "firecrawl_skill.research_domain.research",
    "EvidencePacket": "firecrawl_skill.research_domain.assessment",
    "TerminalDecision": "firecrawl_skill.research_domain.research",
    "HandoffPayload": "firecrawl_skill.research_domain.reporting",
    "BenchmarkDataset": "firecrawl_skill.research_domain.release",
    "BenchmarkObjective": "firecrawl_skill.research_domain.release",
    "BenchmarkSource": "firecrawl_skill.research_domain.release",
    "QualityMeasurement": "firecrawl_skill.research_domain.release",
    "PerformanceMeasurement": "firecrawl_skill.research_domain.release",
    "DeterministicIntegrityCheck": "firecrawl_skill.research_domain.release",
    "WorkflowRunResult": "firecrawl_skill.research_domain.release",
    "WorkflowComparison": "firecrawl_skill.research_domain.release",
    "ReleaseRecommendation": "firecrawl_skill.research_domain.release",
    "TokenAccounting": "firecrawl_skill.research_domain.telemetry",
    "CacheEvent": "firecrawl_skill.research_domain.telemetry",
    "EmbeddingThroughputRecord": "firecrawl_skill.research_domain.telemetry",
    "ResourceSample": "firecrawl_skill.research_domain.telemetry",
    "EndpointUsageRecord": "firecrawl_skill.research_domain.telemetry",
    "PerformanceTelemetrySummary": "firecrawl_skill.research_domain.telemetry",
}


def test_complete_schema_registry_matches_pre_refactor_contract():
    assert {
        version: model.__name__ for version, model in MODEL_BY_VERSION.items()
    } == EXPECTED_MODEL_BY_VERSION
    assert CURRENT_VERSION_BY_MODEL == EXPECTED_CURRENT_VERSION_BY_MODEL
    assert set(VALID) == set(EXPECTED_MODEL_BY_VERSION)
    assert set(schema_registry()) == set(EXPECTED_MODEL_BY_VERSION)


def test_coverage_item_type_registry_owns_python_schema_and_migration_spellings():
    assert tuple((item.name, item.value) for item in CoverageItemType) == (
        COVERAGE_ITEM_TYPE.enum_members()
    )
    coverage_schema = schema_registry()["coverage-ledger-v1"]
    schema_values = coverage_schema["$defs"]["CoverageItem"]["properties"]["item_type"][
        "enum"
    ]
    assert tuple(schema_values) == COVERAGE_ITEM_TYPE.persisted_values()

    alembic = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(alembic)
    revisions = {item.revision for item in script.walk_revisions()}
    COVERAGE_ITEM_TYPE.validate_migration_revisions(revisions)
    assert script.get_heads() == ["0047_coverage_item_type_registry"]


def test_coverage_item_type_registry_requires_a_real_migration_for_new_values():
    alembic = Config(str(ROOT / "alembic.ini"))
    revisions = {
        item.revision for item in ScriptDirectory.from_config(alembic).walk_revisions()
    }
    candidate = replace(
        COVERAGE_ITEM_TYPE,
        current_version=3,
        projection_revisions=COVERAGE_ITEM_TYPE.projection_revisions
        + ("9999_missing_registry_transition",),
        values=COVERAGE_ITEM_TYPE.values
        + (
            PersistedTypeValue(
                "SYNTHETIC_REQUIREMENT",
                "synthetic_requirement",
                "9999_missing_registry_transition",
                3,
            ),
        ),
    )
    with pytest.raises(PersistedTypeRegistryError, match="missing migrations"):
        candidate.validate_migration_revisions(revisions)


def test_new_registry_version_cannot_reuse_an_older_migration():
    with pytest.raises(
        PersistedTypeRegistryError,
        match="must be introduced by their projection revision",
    ):
        replace(
            COVERAGE_ITEM_TYPE,
            current_version=3,
            projection_revisions=COVERAGE_ITEM_TYPE.projection_revisions
            + ("0048_synthetic_registry_transition",),
            values=COVERAGE_ITEM_TYPE.values
            + (
                PersistedTypeValue(
                    "SYNTHETIC_REQUIREMENT",
                    "synthetic_requirement",
                    "0046_exact_source_coverage_item",
                    3,
                ),
            ),
        )


def test_managed_projection_migrations_execute_exact_registry_contract(monkeypatch):
    alembic = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(alembic)

    for version in range(
        COVERAGE_ITEM_TYPE.managed_from_version,
        COVERAGE_ITEM_TYPE.current_version + 1,
    ):
        revision_name = COVERAGE_ITEM_TYPE.managed_projection_revision(version)
        revision = script.get_revision(revision_name)
        assert revision is not None
        module = revision.module
        expected_from_version = (
            version
            if version == COVERAGE_ITEM_TYPE.managed_from_version
            else version - 1
        )
        assert module.REGISTRY_KEY == COVERAGE_ITEM_TYPE.key
        assert module.FROM_REGISTRY_VERSION == expected_from_version
        assert module.REGISTRY_VERSION == version

        executed: list[str] = []

        class Recorder:
            def execute(self, statement: str) -> None:
                executed.append(statement)

        monkeypatch.setattr(module, "op", Recorder())
        module.upgrade()
        assert tuple(executed) == COVERAGE_ITEM_TYPE.postgres_transition_sql(
            expected_from_version, version
        )


def test_coverage_item_type_registry_projects_the_0046_postgres_delta():
    delta = COVERAGE_ITEM_TYPE.postgres_delta(
        COVERAGE_ITEM_TYPE.persisted_values(1), target_version=2
    )
    assert [item.persisted_value for item in delta] == ["exact_source_requirement"]
    assert delta[0].introduced_in_revision == "0046_exact_source_coverage_item"
    assert delta[0].after == "source_requirement"
    assert delta[0].before is None
    transition = COVERAGE_ITEM_TYPE.postgres_transition_sql(1, 2)
    assert transition[1] == (
        "ALTER TYPE coverage_item_type ADD VALUE "
        "'exact_source_requirement' AFTER 'source_requirement';"
    )


def test_persisted_type_registry_anchors_consecutive_leading_additions_safely():
    registry = type(COVERAGE_ITEM_TYPE)(
        key="synthetic_type",
        postgres_type="synthetic_type",
        current_version=2,
        managed_from_version=2,
        projection_revisions=("0002_add_front",),
        values=(
            PersistedTypeValue("NEW_A", "new_a", "0002_add_front", 2),
            PersistedTypeValue("NEW_B", "new_b", "0002_add_front", 2),
            PersistedTypeValue("EXISTING", "existing", "0001_initial", 1),
        ),
    )

    delta = registry.postgres_delta(("existing",), target_version=2)
    assert [(item.persisted_value, item.after, item.before) for item in delta] == [
        ("new_a", None, "existing"),
        ("new_b", "new_a", None),
    ]
    assert registry.postgres_transition_sql(1, 2)[1:3] == (
        "ALTER TYPE synthetic_type ADD VALUE 'new_a' BEFORE 'existing';",
        "ALTER TYPE synthetic_type ADD VALUE 'new_b' AFTER 'new_a';",
    )


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
