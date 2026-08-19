"""Installed-package regressions for issue #264 assessment/reporting slices."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_REQUIRED_FILES = {
    "firecrawl_skill/research_store/assessment/__init__.py",
    "firecrawl_skill/research_store/assessment/audit.py",
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
                coverage = importlib.import_module(
                    "firecrawl_skill.research_store.assessment.coverage"
                )
                reporting = importlib.import_module(
                    "firecrawl_skill.research_store.reporting"
                )
                service = importlib.import_module(
                    "firecrawl_skill.research_store.service"
                )
                root_coverage = importlib.import_module(
                    "firecrawl_skill.research_store.coverage_service"
                )
                root_report = importlib.import_module(
                    "firecrawl_skill.research_store.report_service"
                )
                root_validator = importlib.import_module(
                    "firecrawl_skill.research_store.report_validator"
                )
                root_artifacts = importlib.import_module(
                    "firecrawl_skill.research_store.report_artifact_service"
                )

                assert claims.ClaimManifestService.__module__ == (
                    "firecrawl_skill.research_store.assessment.claims"
                )
                assert audit.AuditService.__module__ == (
                    "firecrawl_skill.research_store.assessment.audit"
                )
                assert service.ClaimManifestService is claims.ClaimManifestService
                assert service.AuditService is audit.AuditService
                assert coverage.CoverageService is root_coverage.CoverageService
                assert reporting.LocalSynthesisService is root_report.LocalSynthesisService
                assert reporting.ReportValidator is root_validator.ReportValidator
                assert reporting.ReportArtifactService is root_artifacts.ReportArtifactService
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
