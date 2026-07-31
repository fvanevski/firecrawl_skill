"""Build a hash-bound exact-head CI evidence artifact from a completed run."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "exact-head-ci-evidence-v1"
REQUIRED_JOB_NAMES = (
    "Release invariants — Python 3.11",
    "Release invariants — Python 3.12",
    "Test — Python 3.11",
    "Test — Python 3.12",
    "Strict campaign contract tests — Python 3.11",
    "Strict campaign contract tests — Python 3.12",
    "Ruff",
)


class ExactHeadCiEvidenceError(RuntimeError):
    """The completed CI run does not satisfy the exact-head contract."""


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ExactHeadCiEvidenceError(f"expected JSON object: {path}")
    return value


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _normalized_jobs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ExactHeadCiEvidenceError("completed CI run lacks a jobs array")
    jobs: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ExactHeadCiEvidenceError("completed CI run contains a non-object job")
        jobs.append(
            {
                "name": str(item.get("name") or ""),
                "conclusion": str(item.get("conclusion") or ""),
                "status": str(item.get("status") or ""),
                "job_id": str(item.get("databaseId") or ""),
                "url": str(item.get("url") or ""),
                "started_at": str(item.get("startedAt") or ""),
                "completed_at": str(item.get("completedAt") or ""),
            }
        )
    return jobs


def validate_completed_ci_run(
    run: Mapping[str, Any],
    *,
    candidate_sha: str,
) -> list[str]:
    errors: list[str] = []
    if str(run.get("headSha") or "") != candidate_sha:
        errors.append("CI run head SHA does not equal the candidate")
    if str(run.get("status") or "") != "completed":
        errors.append("CI run is not completed")
    if str(run.get("conclusion") or "") != "success":
        errors.append("CI run conclusion is not success")
    if not str(run.get("databaseId") or ""):
        errors.append("CI workflow run ID is missing")
    if not _nonempty(run.get("url")):
        errors.append("CI workflow run URL is missing")

    try:
        jobs = _normalized_jobs(run.get("jobs"))
    except ExactHeadCiEvidenceError as error:
        return [*errors, str(error)]

    by_name = {job["name"]: job for job in jobs if job["name"]}
    if set(by_name) != set(REQUIRED_JOB_NAMES):
        errors.append(
            "completed CI job set mismatch: "
            f"expected {sorted(REQUIRED_JOB_NAMES)}, got {sorted(by_name)}"
        )
    for name in REQUIRED_JOB_NAMES:
        job = by_name.get(name)
        if job is None:
            continue
        if job["status"] != "completed":
            errors.append(f"CI job is not completed: {name}")
        if job["conclusion"] != "success":
            errors.append(f"CI job did not succeed: {name}")
        if not job["job_id"]:
            errors.append(f"CI job ID is missing: {name}")
        if not job["url"]:
            errors.append(f"CI job URL is missing: {name}")
    return errors


def build_evidence(
    run: Mapping[str, Any],
    *,
    candidate_sha: str,
    tree_hash: str,
    repository: str,
    dispatcher_run_id: str,
    dispatcher_run_attempt: str,
    dispatcher_workflow_ref: str,
    dispatcher_workflow_sha: str,
) -> dict[str, Any]:
    errors = validate_completed_ci_run(run, candidate_sha=candidate_sha)
    if errors:
        raise ExactHeadCiEvidenceError("; ".join(errors))
    jobs = _normalized_jobs(run.get("jobs"))
    jobs_by_name = {job["name"]: job for job in jobs}
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "candidate_sha": candidate_sha,
        "tree_hash": tree_hash,
        "ci_workflow": {
            "run_id": str(run.get("databaseId")),
            "url": str(run.get("url")),
            "status": str(run.get("status")),
            "conclusion": str(run.get("conclusion")),
            "head_sha": str(run.get("headSha")),
            "jobs": [jobs_by_name[name] for name in REQUIRED_JOB_NAMES],
        },
        "dispatcher_workflow": {
            "run_id": dispatcher_run_id,
            "run_attempt": dispatcher_run_attempt,
            "workflow_ref": dispatcher_workflow_ref,
            "workflow_sha": dispatcher_workflow_sha,
        },
        "gate": "PASS",
        "errors": [],
    }


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-json", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--tree-hash", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--dispatcher-run-id", required=True)
    parser.add_argument("--dispatcher-run-attempt", required=True)
    parser.add_argument("--dispatcher-workflow-ref", required=True)
    parser.add_argument("--dispatcher-workflow-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run = load_object(args.run_json)
    evidence = build_evidence(
        run,
        candidate_sha=args.candidate_sha,
        tree_hash=args.tree_hash,
        repository=args.repository,
        dispatcher_run_id=args.dispatcher_run_id,
        dispatcher_run_attempt=args.dispatcher_run_attempt,
        dispatcher_workflow_ref=args.dispatcher_workflow_ref,
        dispatcher_workflow_sha=args.dispatcher_workflow_sha,
    )
    write_json(args.output, evidence)
    print(
        json.dumps(
            {
                "gate": evidence["gate"],
                "ci_run_id": evidence["ci_workflow"]["run_id"],
                "candidate_sha": evidence["candidate_sha"],
                "output": str(args.output),
                "sha256": sha256_file(args.output),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
