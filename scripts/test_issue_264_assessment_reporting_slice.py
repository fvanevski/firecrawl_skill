"""Structural regressions for issue #264 assessment/reporting locality."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_legacy_service_is_identity_preserving_facade() -> None:
    legacy = importlib.import_module("research_store.service")
    claims = importlib.import_module("research_store.assessment.claims")
    audit = importlib.import_module("research_store.assessment.audit")

    assert legacy.ClaimManifestService is claims.ClaimManifestService
    assert legacy.AuditService is audit.AuditService
    assert claims.ClaimManifestService.__module__.endswith("assessment.claims")
    assert audit.AuditService.__module__.endswith("assessment.audit")

    tree = ast.parse(
        (ROOT / "scripts/research_store/service.py").read_text(encoding="utf-8")
    )
    class_names = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
    }
    assert "ClaimManifestService" not in class_names
    assert "AuditService" not in class_names


def test_assessment_capabilities_have_one_canonical_namespace() -> None:
    coverage = importlib.import_module("research_store.assessment.coverage")
    evidence = importlib.import_module("research_store.assessment.evidence")
    binding = importlib.import_module("research_store.assessment.binding")
    quality = importlib.import_module("research_store.assessment.quality")
    duplicates = importlib.import_module("research_store.assessment.duplicates")
    grouping = importlib.import_module("research_store.assessment.grouping")
    validation = importlib.import_module("research_store.assessment.validation")

    root_coverage = importlib.import_module("research_store.coverage_service")
    root_evidence = importlib.import_module("research_store.evidence")
    root_binding = importlib.import_module("research_store.claim_binding_service")
    root_quality = importlib.import_module("research_store.quality_service")
    root_duplicates = importlib.import_module("research_store.duplicate_service")
    root_grouping = importlib.import_module("research_store.evidence_grouping")
    root_validation = importlib.import_module("research_store.packet_validator")

    assert coverage.CoverageService is root_coverage.CoverageService
    assert evidence.EvidenceService is root_evidence.EvidenceService
    assert binding.ClaimBindingService is root_binding.ClaimBindingService
    assert quality.QualityService is root_quality.QualityService
    assert duplicates.DuplicateGroupService is root_duplicates.DuplicateGroupService
    assert grouping.EvidenceGroupingService is root_grouping.EvidenceGroupingService
    assert validation.EvidencePacketValidator is root_validation.EvidencePacketValidator


def test_reporting_capabilities_have_one_canonical_namespace() -> None:
    construction = importlib.import_module("research_store.reporting.construction")
    validation = importlib.import_module("research_store.reporting.validation")
    artifacts = importlib.import_module("research_store.reporting.artifacts")

    root_construction = importlib.import_module("research_store.report_service")
    root_validation = importlib.import_module("research_store.report_validator")
    root_artifacts = importlib.import_module("research_store.report_artifact_service")

    assert construction.LocalSynthesisService is root_construction.LocalSynthesisService
    assert validation.ReportValidator is root_validation.ReportValidator
    assert artifacts.ReportArtifactService is root_artifacts.ReportArtifactService


def test_completion_and_persistence_authority_stay_outside_topology_facades() -> None:
    completion_source = (
        ROOT / "scripts/research_store/completion_provenance.py"
    ).read_text(encoding="utf-8")
    service_source = (ROOT / "scripts/research_store/service.py").read_text(
        encoding="utf-8"
    )

    assert "load_authoritative_completion_provenance" in completion_source
    assert "completion_provenance" not in service_source
    assert "INSERT " not in service_source.upper()
    assert "UPDATE " not in service_source.upper()
    assert "DELETE " not in service_source.upper()
