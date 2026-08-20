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
                assert evidence.EvidenceService.__module__ == (
                    "firecrawl_skill.research_store.assessment.evidence"
                )
                assert binding.ClaimBindingService.__module__ == (
                    "firecrawl_skill.research_store.assessment.binding"
                )
                assert packet_validation.EvidencePacketValidator.__module__ == (
                    "firecrawl_skill.research_store.assessment.validation"
                )
                assert construction.LocalSynthesisService.__module__ == (
                    "firecrawl_skill.research_store.reporting.construction"
                )
                assert report_validation.ReportValidator.__module__ == (
                    "firecrawl_skill.research_store.reporting.validation"
                )

                for obsolete in (
                    "firecrawl_skill.research_store.service",
                    "firecrawl_skill.research_store.coverage_service",
                    "firecrawl_skill.research_store.quality_service",
                    "firecrawl_skill.research_store.duplicate_service",
                    "firecrawl_skill.research_store.evidence_grouping",
                    "firecrawl_skill.research_store.audit_packet",
                    "firecrawl_skill.research_store.evidence",
                    "firecrawl_skill.research_store.claim_binding_service",
                    "firecrawl_skill.research_store.packet_validator",
                    "firecrawl_skill.research_store.report_service",
                    "firecrawl_skill.research_store.report_validator",
                    "firecrawl_skill.research_store.report_artifact_service",
                ):
                    try:
                        importlib.import_module(obsolete)
                    except ModuleNotFoundError as exc:
                        assert exc.name == obsolete
                    else:
                        raise AssertionError(f"obsolete facade remains importable: {obsolete}")
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
