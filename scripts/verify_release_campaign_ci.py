"""Verify authoritative campaign evidence with completed exact-head CI binding."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from build_exact_head_ci_evidence import REQUIRED_JOB_NAMES, SCHEMA_VERSION
from verify_release_campaign import (
    WorkflowIdentity,
    git,
    load_object,
    sha256_file,
    verify,
    write_json,
)

FINAL_SCHEMA_VERSION = "authoritative-release-evidence-v3"


def validate_exact_head_ci_evidence(
    evidence: Mapping[str, Any],
    *,
    evidence_path: Path,
    expected_sha256: str,
    candidate_sha: str,
    tree_hash: str,
    repository: str,
    ci_run_id: str,
    ci_evidence_run_id: str,
) -> list[str]:
    """Validate the downloaded completed-CI evidence artifact independently."""
    errors: list[str] = []
    if sha256_file(evidence_path) != expected_sha256:
        errors.append("exact-head CI evidence SHA-256 mismatch")
    if evidence.get("schema_version") != SCHEMA_VERSION:
        errors.append("exact-head CI evidence schema mismatch")
    if evidence.get("gate") != "PASS" or evidence.get("errors") != []:
        errors.append("exact-head CI evidence does not record an unqualified PASS")
    if evidence.get("repository") != repository:
        errors.append("exact-head CI evidence repository mismatch")
    if evidence.get("candidate_sha") != candidate_sha:
        errors.append("exact-head CI evidence candidate mismatch")
    if evidence.get("tree_hash") != tree_hash:
        errors.append("exact-head CI evidence tree mismatch")

    ci_workflow = evidence.get("ci_workflow")
    if not isinstance(ci_workflow, Mapping):
        errors.append("exact-head CI evidence lacks ci_workflow")
    else:
        if str(ci_workflow.get("run_id") or "") != ci_run_id:
            errors.append("exact-head CI workflow run ID mismatch")
        if ci_workflow.get("status") != "completed":
            errors.append("exact-head CI workflow is not completed")
        if ci_workflow.get("conclusion") != "success":
            errors.append("exact-head CI workflow did not succeed")
        if ci_workflow.get("head_sha") != candidate_sha:
            errors.append("exact-head CI workflow head SHA mismatch")
        if not str(ci_workflow.get("url") or "").strip():
            errors.append("exact-head CI workflow URL is missing")

        raw_jobs = ci_workflow.get("jobs")
        jobs = (
            [item for item in raw_jobs if isinstance(item, Mapping)]
            if isinstance(raw_jobs, Sequence) and not isinstance(raw_jobs, (str, bytes))
            else []
        )
        by_name = {str(item.get("name") or ""): item for item in jobs}
        if set(by_name) != set(REQUIRED_JOB_NAMES):
            errors.append(
                "exact-head CI job set mismatch: "
                f"expected {sorted(REQUIRED_JOB_NAMES)}, got {sorted(by_name)}"
            )
        for name in REQUIRED_JOB_NAMES:
            job = by_name.get(name)
            if job is None:
                continue
            if job.get("status") != "completed":
                errors.append(f"exact-head CI job is not completed: {name}")
            if job.get("conclusion") != "success":
                errors.append(f"exact-head CI job did not succeed: {name}")
            if not str(job.get("job_id") or "").strip():
                errors.append(f"exact-head CI job ID is missing: {name}")
            if not str(job.get("url") or "").strip():
                errors.append(f"exact-head CI job URL is missing: {name}")

    dispatcher = evidence.get("dispatcher_workflow")
    if not isinstance(dispatcher, Mapping):
        errors.append("exact-head CI evidence lacks dispatcher_workflow")
    else:
        if str(dispatcher.get("run_id") or "") != ci_evidence_run_id:
            errors.append("CI evidence dispatcher run ID mismatch")
        if dispatcher.get("workflow_sha") != candidate_sha:
            errors.append("CI evidence dispatcher workflow SHA mismatch")
        for field in ("run_attempt", "workflow_ref"):
            if not str(dispatcher.get(field) or "").strip():
                errors.append(f"CI evidence dispatcher lacks {field}")
    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("tests/fixtures/benchmark/benchmark-v2.json"),
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--dispatch-sha", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--dispatch-ref", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--workflow-ref", required=True)
    parser.add_argument("--execution-conclusion", required=True)
    parser.add_argument("--ci-evidence", type=Path, required=True)
    parser.add_argument("--ci-evidence-sha256", required=True)
    parser.add_argument("--ci-run-id", required=True)
    parser.add_argument("--ci-evidence-run-id", required=True)
    return parser


def _failure_manifest(args: argparse.Namespace, error: BaseException) -> dict[str, Any]:
    return {
        "schema_version": FINAL_SCHEMA_VERSION,
        "candidate_sha": args.candidate_sha,
        "artifact_retention_days": 90,
        "workflow": {
            "repository": args.repository,
            "run_id": args.run_id,
            "run_attempt": args.run_attempt,
            "workflow_ref": args.workflow_ref,
            "dispatch_ref": args.dispatch_ref,
            "dispatch_sha": args.dispatch_sha,
            "workflow_sha": args.workflow_sha,
            "execution_conclusion": args.execution_conclusion,
        },
        "exact_head_ci": {
            "ci_run_id": args.ci_run_id,
            "ci_evidence_run_id": args.ci_evidence_run_id,
            "evidence_path": str(args.ci_evidence),
            "expected_sha256": args.ci_evidence_sha256,
        },
        "gate": "FAIL",
        "errors": [f"verifier exception: {type(error).__name__}: {error}"],
        "traceback": traceback.format_exc(),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    evidence_path = args.campaign_dir / "release-evidence-manifest.json"
    try:
        identity = WorkflowIdentity(
            candidate_sha=args.candidate_sha,
            dispatch_sha=args.dispatch_sha,
            workflow_sha=args.workflow_sha,
            dispatch_ref=args.dispatch_ref,
            repository=args.repository,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            workflow_ref=args.workflow_ref,
        )
        evidence, errors = verify(
            root=args.campaign_dir,
            dataset_path=args.dataset,
            database_url=args.database_url,
            identity=identity,
            execution_conclusion=args.execution_conclusion,
        )
        tree_hash = str(evidence.get("tree_hash") or git("rev-parse", "HEAD^{tree}"))
        ci_evidence = load_object(args.ci_evidence)
        ci_errors = validate_exact_head_ci_evidence(
            ci_evidence,
            evidence_path=args.ci_evidence,
            expected_sha256=args.ci_evidence_sha256,
            candidate_sha=args.candidate_sha,
            tree_hash=tree_hash,
            repository=args.repository,
            ci_run_id=args.ci_run_id,
            ci_evidence_run_id=args.ci_evidence_run_id,
        )
        errors.extend(ci_errors)
        evidence["schema_version"] = FINAL_SCHEMA_VERSION
        evidence["exact_head_ci"] = {
            "ci_run_id": args.ci_run_id,
            "ci_evidence_run_id": args.ci_evidence_run_id,
            "evidence_path": str(args.ci_evidence.relative_to(args.campaign_dir)),
            "evidence_sha256": sha256_file(args.ci_evidence),
            "evidence": ci_evidence,
            "pass": not ci_errors,
        }
        evidence["errors"] = errors
        evidence["gate"] = "PASS" if not errors else "FAIL"
    except BaseException as error:  # noqa: BLE001
        evidence = _failure_manifest(args, error)
        errors = list(evidence["errors"])

    write_json(evidence_path, evidence)
    campaign_a = evidence.get("campaign_a")
    campaign_b = evidence.get("campaign_b")
    database_runs = evidence.get("database_runs")
    reproducibility = evidence.get("reproducibility")
    summary = {
        "gate": evidence.get("gate"),
        "candidate_sha": args.candidate_sha,
        "workflow_run_id": args.run_id,
        "ci_run_id": args.ci_run_id,
        "ci_evidence_run_id": args.ci_evidence_run_id,
        "campaign_a": (
            campaign_a.get("campaign_id") if isinstance(campaign_a, Mapping) else None
        ),
        "campaign_b": (
            campaign_b.get("campaign_id") if isinstance(campaign_b, Mapping) else None
        ),
        "total_runs": len(database_runs) if isinstance(database_runs, Mapping) else 0,
        "reproducibility": (
            reproducibility.get("pass")
            if isinstance(reproducibility, Mapping)
            else False
        ),
        "manifest": str(evidence_path),
        "manifest_sha256": sha256_file(evidence_path),
    }
    print(json.dumps(summary, indent=2))
    if errors:
        print("\n".join(f"ERROR: {item}" for item in errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
