"""Regression tests for exact-head CI evidence generation."""

from __future__ import annotations

import subprocess
from pathlib import Path

from generate_exact_head_ci_evidence import (
    JOB_FAMILIES,
    REQUIRED_CI_JOBS,
    build_ci_jobs,
    validate_identity,
)

SCRIPTS = Path(__file__).resolve().parent
CI_WORKFLOW = SCRIPTS.parent / ".github" / "workflows" / "ci.yml"


def _successful_results() -> dict[str, str]:
    return {family: "success" for family in JOB_FAMILIES}


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True)
    (path / "README.md").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "candidate",
        ],
        cwd=path,
        check=True,
    )
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=path, text=True
    ).strip()
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", sha],
        cwd=path,
        check=True,
    )
    return sha


def test_build_ci_jobs_expands_completed_dependency_families():
    sha = "a" * 40
    jobs, errors = build_ci_jobs(
        _successful_results(),
        run_id="123",
        run_url="https://github.com/fvanevski/firecrawl_skill/actions/runs/123",
        candidate_sha=sha,
    )
    assert errors == []
    assert tuple(item["name"] for item in jobs) == REQUIRED_CI_JOBS
    assert len(jobs) == 7
    assert {item["conclusion"] for item in jobs} == {"success"}
    assert {item["candidate_sha"] for item in jobs} == {sha}
    assert {item["run_id"] for item in jobs} == {"123"}


def test_build_ci_jobs_fails_closed_on_failed_or_missing_dependency():
    results = _successful_results()
    results["test"] = "failure"
    del results["lint"]
    jobs, errors = build_ci_jobs(
        results,
        run_id="123",
        run_url="https://example.invalid/run/123",
        candidate_sha="a" * 40,
    )
    assert any("test concluded failure" in item for item in errors)
    assert any("lint" in item and "missing" in item for item in errors)
    assert {
        item["conclusion"] for item in jobs if item["source_job_family"] == "test"
    } == {"failure"}


def test_validate_identity_binds_event_checkout_and_origin_main(tmp_path: Path):
    sha = _init_repo(tmp_path)
    identity, errors = validate_identity(
        tmp_path,
        candidate_sha=sha,
        event_sha=sha,
        event_ref="refs/heads/main",
    )
    assert errors == []
    assert identity["candidate_sha"] == sha
    assert identity["checkout_sha"] == sha
    assert identity["origin_main_sha"] == sha
    assert identity["working_tree_clean"] is True


def test_validate_identity_rejects_mismatch_and_dirty_tree(tmp_path: Path):
    sha = _init_repo(tmp_path)
    (tmp_path / "untracked.txt").write_text("dirty", encoding="utf-8")
    _identity, errors = validate_identity(
        tmp_path,
        candidate_sha=sha,
        event_sha="b" * 40,
        event_ref="refs/heads/other",
    )
    assert any("workflow ref is not main" in item for item in errors)
    assert any("event SHA" in item for item in errors)
    assert "candidate checkout is not clean" in errors


def test_ci_uses_dependency_evidence_not_in_progress_run_discovery():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "gh run list" not in workflow
    assert "scripts/generate_exact_head_ci_evidence.py" in workflow
    assert "needs.release-invariants.result" in workflow
    assert "needs.test.result" in workflow
    assert "needs.strict-campaign-contract.result" in workflow
    assert "needs.lint.result" in workflow
    assert "if: always()" in workflow


def test_release_dispatch_requires_explicit_candidate_marker():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "[release-candidate]" in workflow
    assert "gh workflow run release-campaign.yml" in workflow
    assert '-f candidate-sha="$GITHUB_SHA"' in workflow
