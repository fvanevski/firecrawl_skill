#!/usr/bin/env python3
"""Fail-closed verification for authoritative two-mode release campaigns.

The strict campaign runner produces the raw campaign artifacts. This verifier
independently checks the current release contract and writes one canonical,
hash-bound evidence manifest suitable for issue closure without another
repository commit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from research_store.release_benchmark import (  # noqa: E402
    MANDATORY_PERFORMANCE_METRICS,
    MANDATORY_QUALITY_METRICS,
    ReleaseBenchmarkConfig,
)
from smoke_test import RunEvidenceInspector  # noqa: E402

EXPECTED_MODES = ("autonomous_local", "deterministic_debug")
EXPECTED_OBJECTIVE_COUNT = 5
EXPECTED_RUNS_PER_CAMPAIGN = len(EXPECTED_MODES) * EXPECTED_OBJECTIVE_COUNT
EXPECTED_TOTAL_RUNS = EXPECTED_RUNS_PER_CAMPAIGN * 2
_ALLOWED_NOT_APPLICABLE = {("deterministic_debug", "total_tokens")}
_SHA_RE = re.compile(r"[0-9a-f]{40}")


class CampaignVerificationError(RuntimeError):
    """The authoritative campaign evidence does not satisfy the contract."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignVerificationError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CampaignVerificationError(f"JSON artifact is not an object: {path}")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise CampaignVerificationError(
            f"git {' '.join(args)} failed: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout.strip()


def _require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def _metric_map(run: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    records = run.get(key)
    if not isinstance(records, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in records:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name and name not in result:
            result[name] = item
    return result


def _validate_metric(
    *,
    metric: dict[str, Any] | None,
    metric_name: str,
    mode: str,
    run_id: str,
    category: str,
    errors: list[str],
) -> None:
    prefix = f"{mode}/{run_id} {category} metric {metric_name}"
    if metric is None:
        errors.append(f"{prefix} is missing")
        return

    status = str(metric.get("status") or "")
    allowed_na = (mode, metric_name) in _ALLOWED_NOT_APPLICABLE
    if status == "not_applicable":
        _require(allowed_na, f"{prefix} is unexpectedly not_applicable", errors)
        if allowed_na:
            _require(
                metric.get("value") in (None, 0, 0.0),
                f"{prefix} has a nonzero not_applicable value",
                errors,
            )
    else:
        _require(status == "measured", f"{prefix} is {status or 'missing-status'}", errors)
        _require(metric.get("value") is not None, f"{prefix} is measured with null value", errors)

    source = metric.get("source")
    if not isinstance(source, dict):
        errors.append(f"{prefix} has no source provenance")
        return
    _require(str(source.get("run_id") or "") == run_id, f"{prefix} source run_id mismatch", errors)
    for field in ("table", "column", "method"):
        _require(bool(str(source.get(field) or "").strip()), f"{prefix} source lacks {field}", errors)
    _require(bool(str(metric.get("formula") or "").strip()), f"{prefix} lacks formula", errors)


def _validate_run(
    run: dict[str, Any],
    expected_integrity_checks: set[str],
) -> tuple[list[str], str, tuple[str, str]]:
    errors: list[str] = []
    mode = str(run.get("mode") or "")
    objective_id = str(run.get("objective_id") or "")
    run_id = str(run.get("run_id") or "")
    prefix = f"{mode or '<missing-mode>'}/{objective_id or '<missing-objective>'}"

    _require(mode in EXPECTED_MODES, f"{prefix} uses an unexpected execution mode", errors)
    try:
        UUID(run_id)
    except (ValueError, TypeError, AttributeError):
        errors.append(f"{prefix} has invalid research UUID: {run_id!r}")

    run_errors = run.get("errors")
    _require(isinstance(run_errors, list), f"{prefix} errors field is not a list", errors)
    if isinstance(run_errors, list):
        _require(not run_errors, f"{prefix} contains execution errors: {run_errors}", errors)

    quality = _metric_map(run, "quality_metrics")
    performance = _metric_map(run, "performance_metrics")
    for name in sorted(MANDATORY_QUALITY_METRICS):
        _validate_metric(
            metric=quality.get(name),
            metric_name=name,
            mode=mode,
            run_id=run_id,
            category="quality",
            errors=errors,
        )
    for name in sorted(MANDATORY_PERFORMANCE_METRICS):
        _validate_metric(
            metric=performance.get(name),
            metric_name=name,
            mode=mode,
            run_id=run_id,
            category="performance",
            errors=errors,
        )

    # Every emitted metric, including latency and semantic calls, must be
    # authoritative under the same status/value/provenance rules.
    for name, metric in sorted(performance.items()):
        if name not in MANDATORY_PERFORMANCE_METRICS:
            _validate_metric(
                metric=metric,
                metric_name=name,
                mode=mode,
                run_id=run_id,
                category="performance",
                errors=errors,
            )

    integrity = run.get("integrity_checks")
    observed: dict[str, dict[str, Any]] = {}
    if isinstance(integrity, list):
        for item in integrity:
            if not isinstance(item, dict):
                continue
            name = str(item.get("check") or item.get("name") or "")
            if name:
                observed[name] = item
    _require(
        set(observed) == expected_integrity_checks,
        f"{prefix} integrity set mismatch: expected {sorted(expected_integrity_checks)}, "
        f"got {sorted(observed)}",
        errors,
    )
    for name, item in observed.items():
        _require(bool(item.get("passed")), f"{prefix} integrity check failed: {name}", errors)

    return errors, run_id, (mode, objective_id)


def _resolve_result_dir(campaign_dir: Path, manifest_entry: dict[str, Any], label: str) -> Path:
    raw = manifest_entry.get("result_path")
    if raw:
        path = Path(str(raw))
        if path.is_dir():
            return path
    label_root = campaign_dir / label
    candidates = sorted(path for path in label_root.iterdir() if path.is_dir()) if label_root.is_dir() else []
    if not candidates:
        raise CampaignVerificationError(f"campaign {label} result directory is missing")
    return candidates[-1]


def _validate_environment(
    *,
    path: Path,
    candidate_sha: str,
    tree_hash: str,
    dataset_hash: str,
    errors: list[str],
) -> dict[str, Any]:
    environment = _read_json(path)
    _require(environment.get("candidate_sha") == candidate_sha, f"{path} candidate SHA mismatch", errors)
    _require(environment.get("tree_hash") == tree_hash, f"{path} tree hash mismatch", errors)
    _require(environment.get("dataset_hash") == dataset_hash, f"{path} dataset hash mismatch", errors)
    _require(environment.get("strict") is True, f"{path} is not strict", errors)
    for field in (
        "dependency_lock_hash",
        "firecrawl_version",
        "python_version",
        "platform",
        "machine",
        "timestamp",
        "GENERATIVE_MODEL",
        "GENERATIVE_URL",
        "EMBEDDING_MODEL",
        "EMBEDDING_URL",
        "RERANKER_MODEL",
        "RERANKER_URL",
    ):
        _require(bool(str(environment.get(field) or "").strip()), f"{path} lacks {field}", errors)
    return environment


def _verify_database_runs(database_url: str, run_ids: list[str]) -> dict[str, dict[str, Any]]:
    import psycopg

    records: dict[str, dict[str, Any]] = {}
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        for run_id in run_ids:
            cursor.execute(
                """SELECT id::text, status, state, started_at, completed_at
                   FROM research_runs WHERE id = %s""",
                (run_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise CampaignVerificationError(f"research_runs row missing for {run_id}")
            status = str(row[1] or "").lower()
            state = str(row[2] or "").lower()
            if row[4] is None or "completed" not in {status, state}:
                raise CampaignVerificationError(
                    f"research run {run_id} is not completed: status={status!r}, state={state!r}, "
                    f"completed_at={row[4]!r}"
                )
            records[run_id] = {
                "status": status,
                "state": state,
                "started_at": str(row[3]),
                "completed_at": str(row[4]),
            }
    return records


def verify_campaign(
    *,
    campaign_dir: Path,
    dataset_path: Path,
    candidate_sha: str,
    database_url: str,
    output_path: Path,
    retention_days: int,
) -> dict[str, Any]:
    errors: list[str] = []
    if not _SHA_RE.fullmatch(candidate_sha):
        raise CampaignVerificationError("candidate SHA must be 40 lowercase hexadecimal characters")

    head_sha = _git("rev-parse", "HEAD")
    tree_hash = _git("rev-parse", "HEAD^{tree}")
    _require(head_sha == candidate_sha, f"HEAD {head_sha} does not equal candidate {candidate_sha}", errors)
    _require(not _git("status", "--porcelain=v1", "--untracked-files=all"), "checkout is not clean", errors)

    dataset = _read_json(dataset_path)
    objective_ids = [str(item.get("id") or "") for item in dataset.get("objectives", []) if isinstance(item, dict)]
    _require(len(objective_ids) == EXPECTED_OBJECTIVE_COUNT, f"expected {EXPECTED_OBJECTIVE_COUNT} objectives, got {len(objective_ids)}", errors)
    _require(len(set(objective_ids)) == len(objective_ids), "dataset objective IDs are not unique", errors)
    expected_pairs = {(mode, objective_id) for mode in EXPECTED_MODES for objective_id in objective_ids}
    dataset_hash = _sha256_file(dataset_path)

    raw_manifest_path = campaign_dir / "manifest.json"
    raw_manifest = _read_json(raw_manifest_path)
    _require(raw_manifest.get("candidate_sha") == candidate_sha, "raw manifest candidate SHA mismatch", errors)
    _require(raw_manifest.get("tree_hash") == tree_hash, "raw manifest tree hash mismatch", errors)
    _require(raw_manifest.get("dataset_hash") == dataset_hash, "raw manifest dataset hash mismatch", errors)

    expected_integrity_checks = set(ReleaseBenchmarkConfig().integrity_checks)
    campaign_evidence: dict[str, Any] = {}
    all_run_ids: list[str] = []
    inspector = RunEvidenceInspector(database_url)

    for key, label in (("campaign_a", "A"), ("campaign_b", "B")):
        entry = raw_manifest.get(key)
        if not isinstance(entry, dict):
            errors.append(f"raw manifest lacks {key}")
            continue
        result_dir = _resolve_result_dir(campaign_dir, entry, label)
        result_path = result_dir / "result.json"
        environment_path = result_dir / "environment.json"
        result = _read_json(result_path)
        recommendation = result.get("recommendation")
        outcome = recommendation.get("outcome") if isinstance(recommendation, dict) else None
        _require(outcome == "go", f"campaign {label} recommendation is {outcome!r}", errors)
        _require(entry.get("recommendation") == "go", f"raw manifest campaign {label} recommendation is not go", errors)
        _require(entry.get("campaign_id") == result.get("campaign_id"), f"campaign {label} ID mismatch", errors)
        _require(entry.get("result_hash") == _sha256_file(result_path), f"campaign {label} result hash mismatch", errors)

        runs = result.get("runs")
        if not isinstance(runs, list):
            errors.append(f"campaign {label} runs is not a list")
            runs = []
        _require(len(runs) == EXPECTED_RUNS_PER_CAMPAIGN, f"campaign {label} expected {EXPECTED_RUNS_PER_CAMPAIGN} runs, got {len(runs)}", errors)
        observed_pairs: set[tuple[str, str]] = set()
        campaign_run_ids: list[str] = []
        inspections: list[dict[str, Any]] = []
        for run in runs:
            if not isinstance(run, dict):
                errors.append(f"campaign {label} contains a non-object run")
                continue
            run_errors, run_id, pair = _validate_run(run, expected_integrity_checks)
            errors.extend(f"campaign {label}: {item}" for item in run_errors)
            observed_pairs.add(pair)
            campaign_run_ids.append(run_id)
            inspection = inspector.inspect(
                SimpleNamespace(run_id=run_id, mode=pair[0], objective_id=pair[1])
            )
            inspections.append(inspection)
            errors.extend(
                f"campaign {label} {pair[0]}/{pair[1]}: {item}"
                for item in inspection.get("errors", [])
            )
        _require(observed_pairs == expected_pairs, f"campaign {label} run set mismatch", errors)
        _require(len(set(campaign_run_ids)) == EXPECTED_RUNS_PER_CAMPAIGN, f"campaign {label} run UUIDs are not unique", errors)
        _require(entry.get("runs") == EXPECTED_RUNS_PER_CAMPAIGN, f"raw manifest campaign {label} run count mismatch", errors)
        _require(entry.get("run_ids") == campaign_run_ids, f"raw manifest campaign {label} run IDs mismatch", errors)

        environment = _validate_environment(
            path=environment_path,
            candidate_sha=candidate_sha,
            tree_hash=tree_hash,
            dataset_hash=dataset_hash,
            errors=errors,
        )
        campaign_evidence[key] = {
            "campaign_id": result.get("campaign_id"),
            "recommendation": outcome,
            "run_count": len(campaign_run_ids),
            "run_ids": campaign_run_ids,
            "result_path": str(result_path),
            "result_sha256": _sha256_file(result_path),
            "environment_path": str(environment_path),
            "environment_sha256": _sha256_file(environment_path),
            "environment": environment,
            "run_evidence": inspections,
        }
        all_run_ids.extend(campaign_run_ids)

    _require(len(all_run_ids) == EXPECTED_TOTAL_RUNS, f"expected {EXPECTED_TOTAL_RUNS} total runs, got {len(all_run_ids)}", errors)
    _require(len(set(all_run_ids)) == EXPECTED_TOTAL_RUNS, "research UUIDs are not unique across campaigns", errors)

    comparison_candidates = sorted((campaign_dir / "reproducibility").glob("*/comparison.json"))
    if not comparison_candidates:
        errors.append("reproducibility comparison artifact is missing")
        comparison_path = campaign_dir / "reproducibility" / "missing.json"
        comparison: dict[str, Any] = {}
    else:
        comparison_path = comparison_candidates[-1]
        comparison = _read_json(comparison_path)
        _require(comparison.get("all_within_tolerance") is True, "reproducibility did not pass", errors)
        _require(not comparison.get("details"), f"reproducibility has failure details: {comparison.get('details')}", errors)
    raw_repro = raw_manifest.get("reproducibility")
    _require(isinstance(raw_repro, dict) and raw_repro.get("all_within_tolerance") is True, "raw manifest reproducibility did not pass", errors)

    database_records: dict[str, dict[str, Any]] = {}
    if all_run_ids:
        database_records = _verify_database_runs(database_url, all_run_ids)

    evidence = {
        "schema_version": "authoritative-release-evidence-v1",
        "candidate_sha": candidate_sha,
        "tree_hash": tree_hash,
        "working_tree_clean": not bool(_git("status", "--porcelain=v1", "--untracked-files=all")),
        "dataset_path": str(dataset_path),
        "dataset_hash": dataset_hash,
        "dataset_version": dataset.get("version"),
        "execution_modes": list(EXPECTED_MODES),
        "agent_led": {
            "effective": False,
            "disabled_by_environment": os.environ.get("SMOKE_DISABLE_AGENT_LED", "").strip().lower() in {"1", "true", "yes", "on"},
        },
        "objective_ids": objective_ids,
        "expected_runs_per_campaign": EXPECTED_RUNS_PER_CAMPAIGN,
        "expected_total_runs": EXPECTED_TOTAL_RUNS,
        "artifact_retention_days": retention_days,
        "workflow": {
            "repository": os.environ.get("GITHUB_REPOSITORY", ""),
            "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF", ""),
        },
        **campaign_evidence,
        "database_runs": database_records,
        "reproducibility": {
            "pass": comparison.get("all_within_tolerance") is True,
            "comparison_path": str(comparison_path),
            "comparison_sha256": _sha256_file(comparison_path) if comparison_path.is_file() else None,
            "policy_version": comparison.get("policy_version"),
            "relative_tolerance": comparison.get("relative_tolerance"),
            "operational_ratio_limit": comparison.get("operational_ratio_limit"),
            "observations": comparison.get("observations", []),
        },
        "raw_manifest": {
            "path": str(raw_manifest_path),
            "sha256": _sha256_file(raw_manifest_path),
        },
        "gate": "PASS" if not errors else "FAIL",
        "errors": errors,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    if errors:
        raise CampaignVerificationError("authoritative campaign verification failed:\n- " + "\n- ".join(errors))
    return evidence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--retention-days", type=int, default=90)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output or args.campaign_dir / "release-evidence-manifest.json"
    try:
        if not args.database_url:
            raise CampaignVerificationError("DATABASE_URL or --database-url is required")
        evidence = verify_campaign(
            campaign_dir=args.campaign_dir,
            dataset_path=args.dataset,
            candidate_sha=args.candidate_sha,
            database_url=args.database_url,
            output_path=output,
            retention_days=args.retention_days,
        )
        print(
            json.dumps(
                {
                    "gate": evidence["gate"],
                    "candidate_sha": evidence["candidate_sha"],
                    "campaign_a": evidence["campaign_a"]["campaign_id"],
                    "campaign_b": evidence["campaign_b"]["campaign_id"],
                    "total_runs": evidence["expected_total_runs"],
                    "reproducibility": evidence["reproducibility"]["pass"],
                    "manifest": str(output),
                    "manifest_sha256": _sha256_file(output),
                },
                indent=2,
            )
        )
        return 0
    except CampaignVerificationError as exc:
        print(f"RELEASE CAMPAIGN VERIFICATION FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
