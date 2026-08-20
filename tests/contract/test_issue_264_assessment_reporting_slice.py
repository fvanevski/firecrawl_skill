"""Final assessment/reporting locality regressions after issue #269 cleanup."""

from __future__ import annotations

import ast
from pathlib import Path

from firecrawl_skill.research_store.assessment import (
    audit,
    audit_packet,
    binding,
    claims,
    coverage,
    duplicates,
    evidence,
    grouping,
    quality,
    validation,
)
from firecrawl_skill.research_store.postgres_audit import PostgresAuditRepository
from firecrawl_skill.research_store.reporting import artifacts, construction
from firecrawl_skill.research_store.reporting import validation as report_validation

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "src" / "firecrawl_skill" / "research_store"

_OBSOLETE_FLAT_CAPABILITY_PATHS = (
    "service.py",
    "coverage_service.py",
    "quality_service.py",
    "duplicate_service.py",
    "evidence_grouping.py",
    "audit_packet.py",
    "evidence.py",
    "claim_binding_service.py",
    "packet_validator.py",
    "report_service.py",
    "report_validator.py",
    "report_artifact_service.py",
)


def _defined_symbols(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_assessment_services_are_physically_owned_by_assessment_package() -> None:
    expected = {
        claims.ClaimManifestService: ".assessment.claims",
        audit.AuditService: ".assessment.audit",
        coverage.CoverageService: ".assessment.coverage",
        quality.QualityService: ".assessment.quality",
        duplicates.DuplicateGroupService: ".assessment.duplicates",
        grouping.EvidenceGroupingService: ".assessment.grouping",
        evidence.EvidenceService: ".assessment.evidence",
        binding.ClaimBindingService: ".assessment.binding",
        validation.EvidencePacketValidator: ".assessment.validation",
    }
    for symbol, suffix in expected.items():
        assert symbol.__module__.endswith(suffix)

    assert audit_packet.compute_audit_packet_hash_from_db.__module__.endswith(
        ".assessment.audit_packet"
    )
    assert "EvidenceService" in _defined_symbols(STORE / "assessment" / "evidence.py")
    assert "ClaimBindingService" in _defined_symbols(
        STORE / "assessment" / "binding.py"
    )
    assert "EvidencePacketValidator" in _defined_symbols(
        STORE / "assessment" / "validation.py"
    )


def test_reporting_services_are_physically_owned_by_reporting_package() -> None:
    assert construction.LocalSynthesisService.__module__.endswith(
        ".reporting.construction"
    )
    assert report_validation.ReportValidator.__module__.endswith(
        ".reporting.validation"
    )
    assert artifacts.ReportArtifactService.__module__.endswith(".reporting.artifacts")
    assert artifacts.ReportArtifactError.__module__.endswith(".reporting.artifacts")
    assert report_validation.ReportValidationSeverity.__mro__[1] is str

    assert "LocalSynthesisService" in _defined_symbols(
        STORE / "reporting" / "construction.py"
    )
    assert "ReportValidator" in _defined_symbols(STORE / "reporting" / "validation.py")
    assert "ReportArtifactService" in _defined_symbols(
        STORE / "reporting" / "artifacts.py"
    )


def test_flat_assessment_and_reporting_compatibility_paths_are_absent() -> None:
    remaining = [
        name for name in _OBSOLETE_FLAT_CAPABILITY_PATHS if (STORE / name).exists()
    ]
    assert remaining == [], f"obsolete assessment/reporting facades remain: {remaining}"


def test_postgres_audit_repository_is_explicit_infrastructure_not_facade() -> None:
    assert PostgresAuditRepository.__module__.endswith(".postgres_audit")
    source = (STORE / "postgres_audit.py").read_text(encoding="utf-8")
    assert "class PostgresAuditRepository" in source
    assert "Compatibility facade" not in source
    uow_source = (STORE / "postgres_uow_core.py").read_text(encoding="utf-8")
    assert "from .postgres_audit import PostgresAuditRepository" in uow_source


def test_completion_authority_remains_outside_capability_cleanup() -> None:
    source = (STORE / "completion_provenance.py").read_text(encoding="utf-8")
    assert "load_authoritative_completion_provenance" in source
