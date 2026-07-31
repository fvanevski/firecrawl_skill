"""Generate fail-closed exact-head CI evidence from completed job dependencies.

The GitHub Actions workflow supplies aggregate results for each completed job
family through the ``needs`` context. Matrix families are expanded to their
reviewed job display names only after the aggregate result is known. This avoids
querying an in-progress workflow run and binds every result to the current
workflow run and candidate SHA.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from research_store.release_evidence import ReleaseEvidenceGenerator, _manifest_to_dict

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_ALLOWED_RESULTS = frozenset({"success", "failure", "cancelled", "skipped"})
JOB_FAMILIES: dict[str, tuple[str, ...]] = {
    "release-invariants": (
        "Release invariants — Python 3.11",
        "Release invariants — Python 3.12",
    ),
    "test": (
        "Test — Python 3.11",
        "Test — Python 3.12",
    ),
    "strict-campaign-contract": (
        "Strict campaign contract tests — Python 3.11",
        "Strict campaign contract tests — Python 3.12",
    ),
    "lint": ("Ruff",),
}
REQUIRED_CI_JOBS = tuple(
    job_name for family in JOB_FAMILIES.values() for job_name in family
)
REQUIRED_ARTIFACT_CATEGORIES = frozenset({"ci", "benchmark", "source"})
REQUIRED_FINGERPRINT_CATEGORIES = frozenset(
    {
        "environment",
        "dependency",
        "service",
        "model",
        "tokenizer",
        "dataset",
        "ground_truth",
        "hardware",
    }
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout.strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_ci_jobs(
    family_results: Mapping[str, str],
    *,
    run_id: str,
    run_url: str,
    candidate_sha: str,
) -> tuple[list[dict[str, str]], list[str]]:
    """Expand completed dependency families into exact job evidence."""
    jobs: list[dict[str, str]] = []
    errors: list[str] = []
    for family, job_names in JOB_FAMILIES.items():
        conclusion = str(family_results.get(family) or "missing").lower()
        if conclusion not in _ALLOWED_RESULTS:
            errors.append(f"CI dependency {family} has invalid result {conclusion!r}")
        if conclusion != "success":
            errors.append(f"CI dependency {family} concluded {conclusion}")
        for job_name in job_names:
            jobs.append(
                {
                    "name": job_name,
                    "conclusion": conclusion,
                    "run_id": run_id,
                    "url": run_url,
                    "candidate_sha": candidate_sha,
                    "source_job_family": family,
                    "derivation": "github-actions-needs-aggregate-v1",
                }
            )
    return jobs, errors


def validate_identity(
    repo: Path,
    *,
    candidate_sha: str,
    event_sha: str,
    event_ref: str,
) -> tuple[dict[str, Any], list[str]]:
    """Require current main, event SHA, checkout, and clean tree to agree."""
    errors: list[str] = []
    if not _SHA_RE.fullmatch(candidate_sha):
        errors.append("candidate SHA must be exactly 40 lowercase hexadecimal characters")
    head = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    origin_main = _git(repo, "rev-parse", "origin/main")
    if event_ref != "refs/heads/main":
        errors.append(f"workflow ref is not main: {event_ref!r}")
    for label, observed in (
        ("event SHA", event_sha),
        ("checked-out HEAD", head),
        ("origin/main", origin_main),
    ):
        if observed != candidate_sha:
            errors.append(f"{label} {observed!r} does not equal candidate {candidate_sha}")
    if status:
        errors.append("candidate checkout is not clean")
    return (
        {
            "candidate_sha": candidate_sha,
            "event_sha": event_sha,
            "event_ref": event_ref,
            "checkout_sha": head,
            "origin_main_sha": origin_main,
            "tree_hash": tree,
            "working_tree_clean": not bool(status),
        },
        errors,
    )


def validate_generated_manifest(
    manifest: Any,
    repo: Path,
    *,
    candidate_sha: str,
) -> list[str]:
    """Validate immutable artifacts and complete provenance categories."""
    errors: list[str] = []
    if manifest.candidate_sha != candidate_sha:
        errors.append("generated manifest candidate mismatch")
    if not manifest.artifacts:
        errors.append("generated manifest contains no tracked artifacts")
    artifact_categories: set[str] = set()
    for artifact in manifest.artifacts:
        path = repo / artifact.path
        if not path.is_file():
            errors.append(f"manifest artifact is missing: {artifact.path}")
        elif sha256_file(path) != artifact.sha256:
            errors.append(f"manifest artifact hash mismatch: {artifact.path}")
        name = artifact.name.lower()
        logical_path = artifact.path.lower()
        if "ci" in name or ".github" in logical_path or "ci.yml" in logical_path:
            artifact_categories.add("ci")
        elif "release_benchmark" in logical_path or "workflow_benchmark" in logical_path:
            artifact_categories.add("source")
        elif "benchmark" in name or "benchmark" in logical_path:
            artifact_categories.add("benchmark")
    missing_artifacts = sorted(REQUIRED_ARTIFACT_CATEGORIES - artifact_categories)
    if missing_artifacts:
        errors.append(
            "manifest artifact categories are incomplete: "
            + ", ".join(missing_artifacts)
        )
    categories = {item.category for item in manifest.fingerprints}
    missing_categories = sorted(REQUIRED_FINGERPRINT_CATEGORIES - categories)
    if missing_categories:
        errors.append(
            "manifest fingerprint categories are incomplete: "
            + ", ".join(missing_categories)
        )
    return errors


def generate_evidence(
    *,
    repo: Path,
    output: Path,
    candidate_sha: str,
    event_sha: str,
    event_ref: str,
    repository: str,
    run_id: str,
    run_attempt: str,
    workflow_ref: str,
    family_results: Mapping[str, str],
) -> dict[str, Any]:
    """Generate evidence and always write PASS or FAIL output."""
    errors: list[str] = []
    identity: dict[str, Any] = {
        "candidate_sha": candidate_sha,
        "event_sha": event_sha,
        "event_ref": event_ref,
    }
    jobs: list[dict[str, str]] = []
    manifest_dict: dict[str, Any] = {}
    try:
        identity, identity_errors = validate_identity(
            repo,
            candidate_sha=candidate_sha,
            event_sha=event_sha,
            event_ref=event_ref,
        )
        errors.extend(identity_errors)
        run_url = f"https://github.com/{repository}/actions/runs/{run_id}"
        jobs, job_errors = build_ci_jobs(
            family_results,
            run_id=run_id,
            run_url=run_url,
            candidate_sha=candidate_sha,
        )
        errors.extend(job_errors)
        generator_jobs = [
            {
                "name": item["name"],
                "conclusion": item["conclusion"],
                "run_id": item["run_id"],
                "url": item["url"],
            }
            for item in jobs
        ]
        manifest = ReleaseEvidenceGenerator(
            repo, generated_by="github-actions-exact-head"
        ).generate(ci_jobs=generator_jobs)
        errors.extend(
            validate_generated_manifest(
                manifest,
                repo,
                candidate_sha=candidate_sha,
            )
        )
        manifest_dict = _manifest_to_dict(manifest)
        manifest_dict["ci_jobs"] = jobs
    except Exception as exc:  # noqa: BLE001
        errors.append(f"evidence generation failed: {type(exc).__name__}: {exc}")

    evidence = {
        **manifest_dict,
        "schema_version": "exact-head-release-evidence-v2",
        "candidate_sha": candidate_sha,
        "tree_hash": identity.get("tree_hash", ""),
        "working_tree_clean": identity.get("working_tree_clean", False),
        "required_ci_jobs": list(REQUIRED_CI_JOBS),
        "ci_jobs": jobs,
        "workflow": {
            "repository": repository,
            "run_id": run_id,
            "run_attempt": run_attempt,
            "workflow_ref": workflow_ref,
            "event_sha": event_sha,
            "event_ref": event_ref,
        },
        "identity": identity,
        "generated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()
        ),
        "gate": "PASS" if not errors else "FAIL",
        "errors": errors,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return evidence


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate exact-head CI evidence from completed dependencies"
    )
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--event-sha", required=True)
    parser.add_argument("--event-ref", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--workflow-ref", required=True)
    for family in JOB_FAMILIES:
        parser.add_argument(f"--{family}-result", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    family_results = {
        family: getattr(args, family.replace("-", "_") + "_result")
        for family in JOB_FAMILIES
    }
    evidence = generate_evidence(
        repo=args.repo.resolve(),
        output=args.output,
        candidate_sha=args.candidate_sha,
        event_sha=args.event_sha,
        event_ref=args.event_ref,
        repository=args.repository,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        workflow_ref=args.workflow_ref,
        family_results=family_results,
    )
    print(
        json.dumps(
            {
                "gate": evidence["gate"],
                "candidate_sha": evidence["candidate_sha"],
                "tree_hash": evidence["tree_hash"],
                "run_id": evidence["workflow"]["run_id"],
                "output": str(args.output),
                "manifest_sha256": sha256_file(args.output),
            },
            indent=2,
        )
    )
    if evidence["errors"]:
        print(
            "\n".join(f"ERROR: {message}" for message in evidence["errors"]),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
