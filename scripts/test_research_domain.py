from __future__ import annotations

# ruff: noqa: E402 - load the sibling script package without installation.

from copy import deepcopy
import json
from pathlib import Path
import sys
from uuid import UUID

import pytest


SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from research_domain import DomainValidationError, ValidationContext, dumps, load_model
from research_domain.registry import COMPATIBILITY_POLICY, MODEL_BY_VERSION, schema_registry, serialize_model


FIXTURES = ROOT / "tests" / "fixtures" / "research_domain"
SCHEMAS = ROOT / "schemas" / "research-workflow"
VALID = json.loads((FIXTURES / "valid.json").read_text())
INVALID = json.loads((FIXTURES / "invalid.json").read_text())

SPEC_ID = UUID("00000000-0000-0000-0000-000000000100")
QUESTION_ID = UUID("00000000-0000-0000-0000-000000000101")
CLAIM_ID = UUID("00000000-0000-0000-0000-000000000102")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000000301")
RUN_ID = UUID("00000000-0000-0000-0000-000000000401")
COVERAGE_ID = UUID("00000000-0000-0000-0000-000000000402")
PASSAGE_ID = UUID("00000000-0000-0000-0000-000000000601")
SNAPSHOT_ID = UUID("00000000-0000-0000-0000-000000000602")


def _set_path(payload, path, value):
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value


@pytest.mark.parametrize("version", sorted(MODEL_BY_VERSION))
def test_valid_fixtures_round_trip_deterministically(version):
    payload = VALID[version]
    model = load_model(payload)
    assert serialize_model(model) == payload
    assert dumps(model) == dumps(load_model(json.loads(dumps(model))))
    assert json.loads(dumps(model)) == payload


@pytest.mark.parametrize(
    ("version", "case"),
    [(version, case) for version, cases in INVALID.items() for case in cases],
)
def test_invalid_fixture_is_rejected(version, case):
    payload = deepcopy(VALID[version])
    _set_path(payload, case["path"], case["value"])
    with pytest.raises(DomainValidationError):
        load_model(payload)


@pytest.mark.parametrize("version", sorted(MODEL_BY_VERSION))
def test_checked_in_schema_matches_generated_schema(version):
    checked_in = json.loads((SCHEMAS / f"{version}.json").read_text())
    assert checked_in == schema_registry()[version]
    assert checked_in["properties"]["schema_version"]["const"] == version
    assert checked_in["additionalProperties"] is False


def validation_context():
    spec = load_model(VALID["research-spec-v1"])
    ledger = load_model(VALID["coverage-ledger-v1"])
    return ValidationContext(
        research_spec=spec,
        coverage_ledger=ledger,
        run_id=RUN_ID,
        current_run_revision=3,
        current_coverage_revision=2,
        candidate_ids=frozenset({CANDIDATE_ID}),
        snapshot_ids=frozenset({SNAPSHOT_ID}),
        passage_ids=frozenset({PASSAGE_ID}),
    )


@pytest.mark.parametrize(
    ("version", "path", "value", "message"),
    [
        ("search-plan-v1", ["queries", 0, "target_question_ids"], ["00000000-0000-0000-0000-000000009999"], "unknown question IDs"),
        ("candidate-assessment-v1", ["candidate_id"], "00000000-0000-0000-0000-000000009999", "unknown candidate IDs"),
        ("coverage-ledger-v1", ["items", 0, "subject_id"], "00000000-0000-0000-0000-000000009999", "unknown question subject IDs"),
        ("strategy-revision-v1", ["target_coverage_item_ids"], ["00000000-0000-0000-0000-000000009999"], "unknown coverage item IDs"),
        ("evidence-packet-v1", ["passages", 0, "candidate_id"], "00000000-0000-0000-0000-000000009999", "unknown candidate IDs"),
        ("evidence-packet-v1", ["claims", 0, "claim_id"], "00000000-0000-0000-0000-000000009999", "unknown evidence claim IDs"),
        ("evidence-packet-v1", ["unresolved_items"], ["00000000-0000-0000-0000-000000009999"], "unknown coverage item IDs"),
    ],
)
def test_unknown_references_are_rejected(version, path, value, message):
    payload = deepcopy(VALID[version])
    _set_path(payload, path, value)
    with pytest.raises(DomainValidationError, match=message):
        load_model(payload, validation_context())


@pytest.mark.parametrize(
    ("version", "path", "value", "message"),
    [
        ("strategy-revision-v1", ["run_revision"], 2, "stale run revision"),
        ("strategy-revision-v1", ["coverage_revision"], 1, "stale coverage revision"),
        ("evidence-packet-v1", ["coverage_revision"], 1, "stale coverage revision"),
    ],
)
def test_stale_revisions_are_rejected(version, path, value, message):
    payload = deepcopy(VALID[version])
    _set_path(payload, path, value)
    with pytest.raises(DomainValidationError, match=message):
        load_model(payload, validation_context())


def test_internal_evidence_references_are_rejected_without_external_context():
    payload = deepcopy(VALID["evidence-packet-v1"])
    payload["claim_evidence_bindings"][0]["passage_ids"] = [
        "00000000-0000-0000-0000-000000009999"
    ]
    with pytest.raises(DomainValidationError, match="unknown passage IDs"):
        load_model(payload)


def test_evidence_claim_must_exist_in_research_spec():
    payload = deepcopy(VALID["evidence-packet-v1"])
    unknown = "00000000-0000-0000-0000-000000009999"
    payload["claims"][0]["claim_id"] = unknown
    payload["claim_evidence_bindings"][0]["claim_id"] = unknown
    with pytest.raises(DomainValidationError, match="unknown claim IDs"):
        load_model(payload, validation_context())


def test_semantic_uncertainty_and_mechanical_failure_are_separate():
    coverage = VALID["coverage-ledger-v1"]
    assert coverage["items"][0]["status"] == "partially_supported"
    assert coverage["items"][0]["remaining_gap"]
    evidence = VALID["evidence-packet-v1"]
    assert evidence["claims"][0]["semantic_status"] == "qualified"
    assert evidence["claims"][0]["uncertainty"]
    assert evidence["retrieval_provenance"][0]["mechanical_status"] == "succeeded"
    assert evidence["retrieval_provenance"][0]["component_errors"] == []


def test_v1_compatibility_policy_is_explicit_and_rejects_unknown_versions():
    for version, policy in COMPATIBILITY_POLICY.items():
        assert policy == {
            "current": True,
            "readable_versions": (version,),
            "write_version": version,
            "predecessors": (),
        }
        assert load_model(json.loads(json.dumps(VALID[version])))
    payload = deepcopy(VALID["research-spec-v1"])
    payload["schema_version"] = "research-spec-v0"
    with pytest.raises(DomainValidationError, match="unsupported schema_version"):
        load_model(payload)


@pytest.mark.parametrize(
    ("version", "prd_fields"),
    [
        ("research-spec-v1", {"schema_version", "objective", "research_archetype", "risk_level", "execution_mode", "questions", "claims_to_validate", "entities", "jurisdictions", "time_window", "freshness_requirements", "required_source_classes", "corroboration_requirements", "contradiction_requirements", "excluded_interpretations", "structured_data_requirements", "completion_criteria", "user_constraints", "ambiguities", "assumptions"}),
        ("search-plan-v1", {"schema_version", "research_spec_id", "revision", "queries"}),
        ("candidate-assessment-v1", {"schema_version", "candidate_id", "relevance", "source_role", "target_question_ids", "target_claim_ids", "freshness_assessment", "independence_assessment", "extraction_recommendation", "priority", "rationale", "confidence", "uncertainty"}),
        ("coverage-ledger-v1", {"schema_version", "run_id", "revision", "items", "overall_status"}),
        ("strategy-revision-v1", {"schema_version", "run_revision", "decision", "target_coverage_item_ids", "proposed_queries", "proposed_candidate_ids", "proposed_retrieval_queries", "expected_contribution", "estimated_cost", "rationale", "confidence"}),
        ("evidence-packet-v1", {"schema_version", "run_id", "research_spec_id", "coverage_revision", "claims", "passages", "claim_evidence_bindings", "corroborating_groups", "contradicting_groups", "qualifying_groups", "near_duplicate_groups", "source_diversity_summary", "freshness_summary", "limitations", "unresolved_items", "retrieval_provenance"}),
    ],
)
def test_model_and_schema_include_every_prd_field(version, prd_fields):
    schema = schema_registry()[version]
    assert prd_fields <= set(schema["required"])
    assert prd_fields <= set(VALID[version])
