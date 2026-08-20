"""Installed-package regressions for issue #264 assessment/reporting slices."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_REQUIRED_FILES = {
    "firecrawl_skill/research_store/assessment/__init__.py",
    "firecrawl_skill/research_store/assessment/audit.py",
    "firecrawl_skill/research_store/assessment/audit_packet.py",
    "firecrawl_skill/research_store/assessment/binding.py",
    "firecrawl_skill/research_store/assessment/claims.py",
    "firecrawl_skill/research_store/assessment/coverage.py",
    "firecrawl_skill/research_store/assessment/duplicates.py",
    "firecrawl_skill/research_store/assessment/evidence.py",
    "firecrawl_skill/research_store/assessment/grouping.py",
    "firecrawl_skill/research_store/assessment/quality.py",
    "firecrawl_skill/research_store/assessment/validation.py",
    "firecrawl_skill/research_store/reporting/__init__.py",
    "firecrawl_skill/research_store/reporting/artifacts.py",
    "firecrawl_skill/research_store/reporting/construction.py",
    "firecrawl_skill/research_store/reporting/validation.py",
}


def test_assessment_reporting_packages_build_and_import_in_isolation(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(tmp_path),
            str(ROOT),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    wheels = list(tmp_path.glob("firecrawl_skill-1.0.0-*.whl"))
    assert len(wheels) == 1
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        assert _REQUIRED_FILES <= names
        archive.extractall(installed)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(installed)
    isolated = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import importlib

                claims = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.claims"
                )
                audit = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.audit"
                )
                audit_packet = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.audit_packet"
                )
                coverage = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.coverage"
                )
                quality = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.quality"
                )
                duplicates = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.duplicates"
                )
                grouping = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.grouping"
                )
                evidence = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.evidence"
                )
                binding = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.binding"
                )
                packet_validation = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.validation"
                )
                construction = importlib.import_module(
                    "firecrawl_skill.research_store.reporting.construction"
                )
                report_validation = importlib.import_module(
                    "firecrawl_skill.research_store.reporting.validation"
                )
                artifacts = importlib.import_module(
                    "firecrawl_skill.research_store.reporting.artifacts"
                )

                service = importlib.import_module(
                    "firecrawl_skill.research_store.service"
                )
                root_coverage = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.coverage"
                )
                root_quality = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.quality"
                )
                root_duplicates = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.duplicates"
                )
                root_grouping = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.grouping"
                )
                root_audit_packet = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.audit_packet"
                )
                root_evidence = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.evidence"
                )
                root_binding = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.binding"
                )
                root_packet_validation = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.validation"
                )
                root_report = importlib.import_module(
                    "firecrawl_skill.research_store.reporting.construction"
                )
                root_report_validation = importlib.import_module(
                    "firecrawl_skill.research_store.reporting.validation"
                )
                root_artifacts = importlib.import_module(
                    "firecrawl_skill.research_store.reporting.artifacts"
                )

                assert claims.ClaimManifestService.__module__ == (
                    "firecrawl_skill.research_store.assessment.claims"
                )
                assert audit.AuditService.__module__ == (
                    "firecrawl_skill.research_store.assessment.audit"
                )
                assert coverage.CoverageService.__module__ == (
                    "firecrawl_skill.research_store.assessment.coverage"
                )
                assert quality.QualityService.__module__ == (
                    "firecrawl_skill.research_store.assessment.quality"
                )
                assert duplicates.DuplicateGroupService.__module__ == (
                    "firecrawl_skill.research_store.assessment.duplicates"
                )
                assert grouping.EvidenceGroupingService.__module__ == (
                    "firecrawl_skill.research_store.assessment.grouping"
                )
                assert audit_packet.compute_audit_packet_hash_from_db.__module__ == (
                    "firecrawl_skill.research_store.assessment.audit_packet"
                )
                assert artifacts.ReportArtifactService.__module__ == (
                    "firecrawl_skill.research_store.reporting.artifacts"
                )

                assert service.ClaimManifestService is claims.ClaimManifestService
                assert service.AuditService is audit.AuditService
                assert root_coverage.CoverageService is coverage.CoverageService
                assert root_quality.QualityService is quality.QualityService
                assert root_duplicates.DuplicateGroupService is duplicates.DuplicateGroupService
                assert root_grouping.EvidenceGroupingService is grouping.EvidenceGroupingService
                assert (
                    root_audit_packet.compute_audit_packet_hash_from_db
                    is audit_packet.compute_audit_packet_hash_from_db
                )
                assert evidence.EvidenceService is root_evidence.EvidenceService
                assert binding.ClaimBindingService is root_binding.ClaimBindingService
                assert (
                    packet_validation.EvidencePacketValidator
                    is root_packet_validation.EvidencePacketValidator
                )
                assert construction.LocalSynthesisService is root_report.LocalSynthesisService
                assert (
                    report_validation.ReportValidator
                    is root_report_validation.ReportValidator
                )
                assert artifacts.ReportArtifactService is root_artifacts.ReportArtifactService
                """
            ),
        ],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert isolated.returncode == 0, isolated.stderr
