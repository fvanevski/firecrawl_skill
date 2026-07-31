"""Create a minimal durable FAIL manifest when verification produced none."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SCHEMA_VERSION = "authoritative-release-evidence-v3"


def ensure_failure_manifest(
    *,
    manifest: Path,
    candidate_sha: str,
    repository: str,
    workflow_run_id: str,
    workflow_run_attempt: str,
    workflow_ref: str,
    workflow_sha: str,
    ci_run_id: str,
    ci_evidence_run_id: str,
    ci_evidence_sha256: str,
) -> bool:
    """Write a minimal failure manifest only when the verifier wrote none."""
    if manifest.is_file():
        return False
    manifest.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "schema_version": SCHEMA_VERSION,
        "candidate_sha": candidate_sha,
        "artifact_retention_days": 90,
        "workflow": {
            "repository": repository,
            "run_id": workflow_run_id,
            "run_attempt": workflow_run_attempt,
            "workflow_ref": workflow_ref,
            "workflow_sha": workflow_sha,
        },
        "exact_head_ci": {
            "ci_run_id": ci_run_id,
            "ci_evidence_run_id": ci_evidence_run_id,
            "expected_sha256": ci_evidence_sha256,
        },
        "gate": "FAIL",
        "errors": ["campaign verifier did not produce a manifest"],
    }
    temporary = manifest.with_name(f".{manifest.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest)
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--workflow-run-attempt", required=True)
    parser.add_argument("--workflow-ref", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--ci-run-id", required=True)
    parser.add_argument("--ci-evidence-run-id", required=True)
    parser.add_argument("--ci-evidence-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    created = ensure_failure_manifest(
        manifest=args.manifest,
        candidate_sha=args.candidate_sha,
        repository=args.repository,
        workflow_run_id=args.workflow_run_id,
        workflow_run_attempt=args.workflow_run_attempt,
        workflow_ref=args.workflow_ref,
        workflow_sha=args.workflow_sha,
        ci_run_id=args.ci_run_id,
        ci_evidence_run_id=args.ci_evidence_run_id,
        ci_evidence_sha256=args.ci_evidence_sha256,
    )
    print("created" if created else "preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
