"""Behavioral tests for completed exact-head CI evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from build_exact_head_ci_evidence import (
    REQUIRED_JOB_NAMES,
    ExactHeadCiEvidenceError,
    build_evidence,
    validate_completed_ci_run,
)

ROOT = Path(__file__).resolve().parent.parent
DISPATCH_WORKFLOW = ROOT / ".github" / "workflows" / "release-campaign-dispatch.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release-campaign.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _completed_run(*, candidate_sha: str = "a" * 40) -> dict:
    return {
        "databaseId": 123456,
        "headSha": candidate_sha,
        "status": "completed",
        "conclusion": "success",
        "url": "https://github.example/actions/runs/123456",
        "jobs": [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "databaseId": index + 1,
                "url": f"https://github.example/actions/jobs/{index + 1}",
                "startedAt": "2026-07-31T00:00:00Z",
                "completedAt": "2026-07-31T00:01:00Z",
            }
            for index, name in enumerate(REQUIRED_JOB_NAMES)
        ],
    }


def test_completed_ci_run_accepts_exact_required_job_set():
    run = _completed_run()
    assert not validate_completed_ci_run(run, candidate_sha="a" * 40)


def test_completed_ci_run_rejects_wrong_sha_and_failed_job():
    run = _completed_run(candidate_sha="b" * 40)
    run["jobs"][0]["conclusion"] = "failure"
    errors = validate_completed_ci_run(run, candidate_sha="a" * 40)
    assert any("head SHA" in item for item in errors)
    assert any("did not succeed" in item for item in errors)


def test_completed_ci_run_rejects_missing_or_extra_jobs():
    run = _completed_run()
    run["jobs"].pop()
    errors = validate_completed_ci_run(run, candidate_sha="a" * 40)
    assert any("job set mismatch" in item for item in errors)


def test_evidence_binds_ci_and_dispatcher_identity():
    run = _completed_run()
    evidence = build_evidence(
        run,
        candidate_sha="a" * 40,
        tree_hash="b" * 40,
        repository="fvanevski/firecrawl_skill",
        dispatcher_run_id="789",
        dispatcher_run_attempt="1",
        dispatcher_workflow_ref=(
            "fvanevski/firecrawl_skill/.github/workflows/"
            "release-campaign-dispatch.yml@refs/heads/main"
        ),
        dispatcher_workflow_sha="a" * 40,
    )
    assert evidence["gate"] == "PASS"
    assert evidence["candidate_sha"] == "a" * 40
    assert evidence["tree_hash"] == "b" * 40
    assert evidence["ci_workflow"]["run_id"] == "123456"
    assert evidence["dispatcher_workflow"]["run_id"] == "789"
    assert [item["name"] for item in evidence["ci_workflow"]["jobs"]] == list(
        REQUIRED_JOB_NAMES
    )


def test_evidence_builder_fails_closed():
    run = _completed_run()
    run["conclusion"] = "failure"
    with pytest.raises(ExactHeadCiEvidenceError):
        build_evidence(
            run,
            candidate_sha="a" * 40,
            tree_hash="b" * 40,
            repository="fvanevski/firecrawl_skill",
            dispatcher_run_id="789",
            dispatcher_run_attempt="1",
            dispatcher_workflow_ref="workflow-ref",
            dispatcher_workflow_sha="a" * 40,
        )


def test_ci_contains_only_the_seven_ordinary_jobs():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    for name in REQUIRED_JOB_NAMES:
        assert name.replace("Python 3.11", "Python ${{ matrix.python-version }}").replace(
            "Python 3.12", "Python ${{ matrix.python-version }}"
        ) in workflow
    assert "release-evidence:" not in workflow
    assert "dispatch-release-campaign:" not in workflow


def test_completed_run_dispatcher_is_hash_bound_and_one_use():
    workflow = DISPATCH_WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_run:" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "Fix exact-head CI evidence and campaign dispatch" in workflow
    assert "gh run view \"$CI_RUN_ID\"" in workflow
    assert "build_exact_head_ci_evidence.py" in workflow
    assert "retention-days: 90" in workflow
    assert "ci-evidence-sha256" in workflow


def test_release_workflow_consumes_exact_ci_artifact():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "ci-run-id:" in workflow
    assert "ci-evidence-run-id:" in workflow
    assert "ci-evidence-sha256:" in workflow
    assert "gh run download" in workflow
    assert "--ci-evidence" in workflow
    assert "--ci-evidence-sha256" in workflow
