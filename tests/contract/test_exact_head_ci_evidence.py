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

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
CI_WORKFLOW = SCRIPTS.parent / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = SCRIPTS.parent / ".github" / "workflows" / "release-campaign.yml"


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
    assert len(jobs) == 8
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


def test_build_ci_jobs_fails_closed_on_pyrefly_failure():
    results = _successful_results()
    results["typecheck"] = "failure"
    jobs, errors = build_ci_jobs(
        results,
        run_id="123",
        run_url="https://example.invalid/run/123",
        candidate_sha="a" * 40,
    )
    assert "CI dependency typecheck concluded failure" in errors
    typecheck_jobs = [item for item in jobs if item["source_job_family"] == "typecheck"]
    assert [item["name"] for item in typecheck_jobs] == ["Pyrefly"]
    assert {item["conclusion"] for item in typecheck_jobs} == {"failure"}


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
    assert "scripts/ci_merge_gate.py" in workflow
    assert "needs.plan.result" in workflow
    assert "needs.pyrefly.result" in workflow
    assert "needs.core.result" in workflow
    assert "needs.profiles.result" in workflow
    assert "if: always()" in workflow


def test_release_campaign_is_manual_exact_main_not_automatic_ci_dispatch():
    ci_workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    release_workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "gh workflow run release-campaign.yml" not in ci_workflow
    assert "[release-candidate]" not in ci_workflow
    assert "workflow_dispatch:" in release_workflow
    assert "pull_request:" not in release_workflow
    assert 'test "$DISPATCH_REF" = "refs/heads/main"' in release_workflow
    assert 'test "$DISPATCH_SHA" = "$CANDIDATE_SHA"' in release_workflow
    assert 'test "$WORKFLOW_SHA" = "$CANDIDATE_SHA"' in release_workflow


def test_release_campaign_initializes_campaign_dir_at_runner_time():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "CAMPAIGN_DIR: ${{ runner.temp }}" not in workflow
    assert '"$RUNNER_TEMP/release-campaign-$CANDIDATE_SHA"' in workflow
    assert '>> "$GITHUB_ENV"' in workflow


def test_release_campaign_binds_complete_runtime_identity():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "VALKEY_URL: ${{ secrets.VALKEY_URL }}" in workflow
    assert "EMBEDDING_MODEL: ${{ vars.EMBEDDING_MODEL }}" in workflow
    assert "EMBEDDING_REVISION: ${{ vars.EMBEDDING_REVISION }}" in workflow
    assert "EMBEDDING_DIMENSION: ${{ vars.EMBEDDING_DIMENSION }}" in workflow
    assert "QDRANT_ALIAS: ${{ vars.QDRANT_ALIAS }}" in workflow
