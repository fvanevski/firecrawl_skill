"""Verify and hash-bind an authoritative full release campaign.

This verifier is independent from campaign execution. It derives the required
two-mode x five-objective shape from the dataset and validates serialized
artifacts, PostgreSQL completion state, substantive run evidence, metric
provenance, integrity checks, reproducibility, and exact-head workflow identity.
It always attempts to emit ``release-evidence-manifest.json`` so failed
validation remains durable evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import traceback
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

EXPECTED_MODES = ("autonomous_local", "deterministic_debug")
EXPECTED_OBJECTIVES = 5
EXPECTED_RUNS_PER_CAMPAIGN = 10
EXPECTED_TOTAL_RUNS = 20
ALLOWED_NOT_APPLICABLE = frozenset({("deterministic_debug", "total_tokens")})
_SHA_RE = re.compile(r"[0-9a-f]{40}")
BASE_PROVENANCE_FIELDS = ("table", "column", "run_id", "method")
# Secret-safe environment identity fields.
# Raw endpoint URLs (GENERATIVE_URL, EMBEDDING_URL, RERANKER_URL) must never
# appear in release evidence; they are injected from GitHub Secrets at runtime
# and their absence proves the security contract is respected.
ENVIRONMENT_FIELDS = (
    "candidate_sha",
    "tree_hash",
    "dataset_hash",
    "dependency_lock_hash",
    "firecrawl_version",
    "python_version",
    "platform",
    "machine",
    "timestamp",
    # Safe model/revision/dimension identity — no raw URLs.
    "GENERATIVE_MODEL",
    "EMBEDDING_MODEL",
    "EMBEDDING_REVISION",
    "EMBEDDING_DIMENSION",
    "RERANKER_MODEL",
)


class VerificationError(RuntimeError):
    """Malformed verifier input prevented ordinary validation."""


@dataclass(frozen=True)
class WorkflowIdentity:
    candidate_sha: str
    dispatch_sha: str
    workflow_sha: str
    dispatch_ref: str
    repository: str
    run_id: str
    run_attempt: str
    workflow_ref: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise VerificationError(f"expected JSON object: {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise VerificationError(
            f"git {' '.join(args)} failed: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout.strip()


def validate_workflow_identity(
    identity: WorkflowIdentity,
    *,
    checkout_sha: str,
    tree_hash: str,
    working_tree_clean: bool,
) -> list[str]:
    errors: list[str] = []
    sha_values = {
        "candidate": identity.candidate_sha,
        "dispatch": identity.dispatch_sha,
        "workflow": identity.workflow_sha,
        "checkout": checkout_sha,
    }
    for name, value in sha_values.items():
        if not _SHA_RE.fullmatch(value):
            errors.append(f"{name} SHA is not a full lowercase 40-character SHA")
    if len(set(sha_values.values())) != 1:
        errors.append(f"workflow identity mismatch: {sha_values}")
    if identity.dispatch_ref != "refs/heads/main":
        errors.append(
            f"workflow dispatch ref must be refs/heads/main, got {identity.dispatch_ref!r}"
        )
    if not identity.repository:
        errors.append("workflow repository identity is missing")
    if not identity.run_id or not identity.run_attempt or not identity.workflow_ref:
        errors.append("workflow run identity is incomplete")
    if not _SHA_RE.fullmatch(tree_hash):
        errors.append("checkout tree hash is invalid")
    if not working_tree_clean:
        errors.append("candidate checkout is not clean")
    return errors


def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def validate_environment(
    environment: Mapping[str, Any],
    *,
    campaign_label: str,
    identity: WorkflowIdentity,
    tree_hash: str,
    dataset_hash: str,
) -> list[str]:
    """Validate an environment manifest against release-campaign requirements.

    Returns a list of error strings; empty means valid.
    """
    errors: list[str] = []
    for field in ENVIRONMENT_FIELDS:
        if not _nonempty(environment.get(field)):
            errors.append(f"campaign {campaign_label} environment lacks {field}")
    if environment.get("candidate_sha") != identity.candidate_sha:
        errors.append(f"campaign {campaign_label} environment candidate mismatch")
    if environment.get("tree_hash") != tree_hash:
        errors.append(f"campaign {campaign_label} environment tree mismatch")
    if environment.get("dataset_hash") != dataset_hash:
        errors.append(f"campaign {campaign_label} environment dataset mismatch")
    if environment.get("strict") is not True:
        errors.append(f"campaign {campaign_label} environment is not strict")
    if environment.get("execution_modes") != list(EXPECTED_MODES):
        errors.append(
            f"campaign {campaign_label} environment modes are not authoritative"
        )
    return errors


def validate_metric_record(
    metric: Mapping[str, Any],
    *,
    mode: str,
    run_id: str,
    quality: bool,
) -> list[str]:
    name = str(metric.get("name") or "")
    prefix = f"metric {name or '<unnamed>'}"
    errors: list[str] = []
    status = str(metric.get("status") or "")
    allowed_na = (mode, name) in ALLOWED_NOT_APPLICABLE and not quality

    if status == "not_applicable":
        if not allowed_na:
            errors.append(f"{prefix} is unexpectedly not_applicable")
        if metric.get("value") is not None:
            errors.append(f"{prefix} is not_applicable but has a value")
    elif status != "measured":
        errors.append(f"{prefix} status is {status!r}, expected measured")
    elif metric.get("value") is None:
        errors.append(f"{prefix} is measured with a null value")

    if not _nonempty(metric.get("formula")):
        errors.append(f"{prefix} lacks a formula")

    source = metric.get("source")
    if not isinstance(source, Mapping):
        return [*errors, f"{prefix} lacks provenance"]
    for field in BASE_PROVENANCE_FIELDS:
        if not _nonempty(source.get(field)):
            errors.append(f"{prefix} provenance lacks {field}")
    if str(source.get("run_id") or "") != run_id:
        errors.append(f"{prefix} provenance run_id does not match the run")

    record_ids = source.get("record_ids")
    sample_count = source.get("sample_count")
    has_records = (
        isinstance(record_ids, Sequence)
        and not isinstance(record_ids, (str, bytes))
        and bool(record_ids)
    )
    has_samples = isinstance(sample_count, int) and sample_count > 0
    if status == "measured" and not (has_records or has_samples):
        errors.append(f"{prefix} provenance has no authoritative records or samples")

    if name == "cache_hit_rate":
        if not has_records:
            errors.append(f"{prefix} lacks run-scoped cache event IDs")
        if not _nonempty(source.get("stages")):
            errors.append(f"{prefix} lacks cache stages")
        if not _nonempty(source.get("stage_set_version")):
            errors.append(f"{prefix} lacks cache stage-set version")
    elif name == "cpu_percent" and status == "measured":
        if not isinstance(sample_count, int) or sample_count < 2:
            errors.append(f"{prefix} requires at least two samples")
        for field in ("collector", "collector_version"):
            if not _nonempty(source.get(field)):
                errors.append(f"{prefix} provenance lacks {field}")
    elif name == "gpu_memory_mb" and status == "measured":
        if not isinstance(sample_count, int) or sample_count < 3:
            errors.append(f"{prefix} requires at least three samples")
        for field in ("collector", "collector_version", "device_uuid"):
            if not _nonempty(source.get(field)):
                errors.append(f"{prefix} provenance lacks {field}")
    elif name == "total_tokens" and status == "not_applicable":
        if mode != "deterministic_debug":
            errors.append(f"{prefix} N/A is not mode-scoped to deterministic_debug")
        if not _nonempty(source.get("status_counts")):
            errors.append(f"{prefix} N/A lacks status-count provenance")
    return errors


def validate_run_shape(
    runs: Sequence[Mapping[str, Any]],
    *,
    objective_ids: Sequence[str],
) -> list[str]:
    errors: list[str] = []
    expected_pairs = {
        (mode, objective_id)
        for mode in EXPECTED_MODES
        for objective_id in objective_ids
    }
    actual_pairs = {
        (str(run.get("mode") or ""), str(run.get("objective_id") or "")) for run in runs
    }
    if len(runs) != EXPECTED_RUNS_PER_CAMPAIGN:
        errors.append(f"expected {EXPECTED_RUNS_PER_CAMPAIGN} runs, got {len(runs)}")
    if actual_pairs != expected_pairs:
        errors.append(
            f"run set mismatch: expected {sorted(expected_pairs)}, got {sorted(actual_pairs)}"
        )
    run_ids = [str(run.get("run_id") or "") for run in runs]
    if len(set(run_ids)) != EXPECTED_RUNS_PER_CAMPAIGN:
        errors.append("campaign run UUIDs are not unique")
    for run_id in run_ids:
        try:
            UUID(run_id)
        except (TypeError, ValueError, AttributeError):
            errors.append(f"invalid research UUID: {run_id!r}")
    return errors


def validate_reproducibility(comparison: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if comparison.get("all_within_tolerance") is not True:
        errors.append("reproducibility did not pass")
    if comparison.get("details"):
        errors.append(f"reproducibility contains failures: {comparison.get('details')}")
    if not _nonempty(comparison.get("policy_version")):
        errors.append("reproducibility policy version is missing")
    if comparison.get("relative_tolerance") is None:
        errors.append("reproducibility relative tolerance is missing")
    if comparison.get("operational_ratio_limit") is None:
        errors.append("reproducibility operational ratio limit is missing")
    return errors


def validate_timing_diagnostics(
    diagnostics: Mapping[str, Any],
    *,
    candidate_sha: str,
    run_ids: Sequence[str],
) -> list[str]:
    """Validate durable PostgreSQL-derived timing diagnostics."""
    errors: list[str] = []
    if diagnostics.get("schema_version") != "release-campaign-timing-v1":
        errors.append("timing diagnostics schema version is invalid")
    if diagnostics.get("candidate_sha") != candidate_sha:
        errors.append("timing diagnostics candidate mismatch")

    raw_runs = diagnostics.get("runs")
    runs = (
        [item for item in raw_runs if isinstance(item, Mapping)]
        if isinstance(raw_runs, list)
        else []
    )
    expected_ids = set(run_ids)
    actual_ids = {str(item.get("run_id") or "") for item in runs}
    if actual_ids != expected_ids or len(runs) != len(expected_ids):
        errors.append(
            "timing diagnostics run set mismatch: "
            f"expected {sorted(expected_ids)}, got {sorted(actual_ids)}"
        )
    if diagnostics.get("run_count") != len(runs):
        errors.append("timing diagnostics run_count does not match runs")

    for run in runs:
        run_id = str(run.get("run_id") or "")
        if run.get("state") != "completed":
            errors.append(f"timing diagnostics run {run_id} is not completed")
        if run.get("duration_ms") is None:
            errors.append(f"timing diagnostics run {run_id} lacks duration")
        if not isinstance(run.get("semantic_stage_totals"), Mapping):
            errors.append(f"timing diagnostics run {run_id} lacks stage totals")

    if not isinstance(diagnostics.get("reproducibility_failures"), list):
        errors.append("timing diagnostics reproducibility failures are malformed")
    return errors


def _safe_result_dir(root: Path, entry: Mapping[str, Any], label: str) -> Path:
    result_dir = Path(str(entry.get("result_path") or ""))
    if not result_dir.is_dir():
        candidates = sorted((root / label).glob("*/result.json"))
        if not candidates:
            raise VerificationError(f"campaign {label} result is missing")
        result_dir = candidates[-1].parent
    root_resolved = root.resolve()
    result_resolved = result_dir.resolve()
    if root_resolved not in result_resolved.parents:
        raise VerificationError(f"campaign {label} result path escapes campaign root")
    return result_resolved


def _metric_map(value: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, list):
        return {}
    return {
        str(item.get("name")): item
        for item in value
        if isinstance(item, Mapping) and item.get("name")
    }


def _integrity_map(value: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, list):
        return {}
    return {
        str(item.get("check") or item.get("name")): item
        for item in value
        if isinstance(item, Mapping) and (item.get("check") or item.get("name"))
    }


def _database_completion(
    database_url: str, run_ids: Iterable[str]
) -> tuple[dict[str, Any], list[str]]:
    import psycopg

    records: dict[str, Any] = {}
    errors: list[str] = []
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        for run_id in run_ids:
            cursor.execute(
                """SELECT state, started_at, completed_at
                   FROM research_runs WHERE id = %s""",
                (run_id,),
            )
            row = cursor.fetchone()
            if row is None:
                errors.append(f"research_runs row is missing for {run_id}")
                continue
            state = str(row[0] or "").lower()
            completed = row[2] is not None and state == "completed"
            if not completed:
                errors.append(
                    f"run {run_id} is not completed: "
                    f"state={state!r}, completed_at={row[2]!r}"
                )
            records[run_id] = {
                # Retain the evidence field for schema compatibility while
                # deriving it only from the authoritative lifecycle state.
                "status": state,
                "state": state,
                "started_at": str(row[1]),
                "completed_at": str(row[2]),
                "orchestration_outcome": "completed" if completed else "incomplete",
                "orchestration_outcome_source": "research_runs.state",
            }
    return records, errors


def verify(
    *,
    root: Path,
    dataset_path: Path,
    database_url: str,
    identity: WorkflowIdentity,
    execution_conclusion: str,
) -> tuple[dict[str, Any], list[str]]:
    from smoke_test import RunEvidenceInspector

    from firecrawl_skill.research_store.release.benchmark import (
        MANDATORY_PERFORMANCE_METRICS,
        MANDATORY_QUALITY_METRICS,
        ReleaseBenchmarkConfig,
    )

    errors: list[str] = []
    checkout_sha = git("rev-parse", "HEAD")
    tree_hash = git("rev-parse", "HEAD^{tree}")
    dirty = git("status", "--porcelain=v1", "--untracked-files=all")
    errors.extend(
        validate_workflow_identity(
            identity,
            checkout_sha=checkout_sha,
            tree_hash=tree_hash,
            working_tree_clean=not bool(dirty),
        )
    )
    if execution_conclusion != "success":
        errors.append(f"campaign execution step concluded {execution_conclusion!r}")

    dataset = load_object(dataset_path)
    raw_objectives = dataset.get("objectives")
    objectives = raw_objectives if isinstance(raw_objectives, list) else []
    objective_ids = [
        str(item.get("id") or "")
        for item in objectives
        if isinstance(item, Mapping)
    ]
    if (
        len(objective_ids) != EXPECTED_OBJECTIVES
        or len(set(objective_ids)) != EXPECTED_OBJECTIVES
    ):
        errors.append(f"expected five unique objectives, got {objective_ids}")
    dataset_hash = sha256_file(dataset_path)

    raw_manifest_path = root / "manifest.json"
    raw_manifest = load_object(raw_manifest_path)
    if raw_manifest.get("candidate_sha") != identity.candidate_sha:
        errors.append("raw manifest candidate mismatch")
    if raw_manifest.get("tree_hash") != tree_hash:
        errors.append("raw manifest tree mismatch")
    if raw_manifest.get("dataset_hash") != dataset_hash:
        errors.append("raw manifest dataset mismatch")
    if raw_manifest.get("modes") != list(EXPECTED_MODES):
        errors.append(f"raw manifest modes are not exactly {list(EXPECTED_MODES)}")

    expected_checks = set(ReleaseBenchmarkConfig().integrity_checks)
    inspector = RunEvidenceInspector(database_url)
    campaigns: dict[str, Any] = {}
    all_run_ids: list[str] = []

    for key, label in (("campaign_a", "A"), ("campaign_b", "B")):
        entry = raw_manifest.get(key)
        if not isinstance(entry, Mapping):
            errors.append(f"raw manifest lacks {key}")
            continue
        result_dir = _safe_result_dir(root, entry, label)
        result_path = result_dir / "result.json"
        environment_path = result_dir / "environment.json"
        result = load_object(result_path)
        environment = load_object(environment_path)

        recommendation = result.get("recommendation")
        outcome = (
            recommendation.get("outcome")
            if isinstance(recommendation, Mapping)
            else None
        )
        if outcome != "go" or entry.get("recommendation") != "go":
            errors.append(f"campaign {label} recommendation is not exactly go")
        if entry.get("campaign_id") != result.get("campaign_id"):
            errors.append(f"campaign {label} ID mismatch")
        if entry.get("result_hash") != sha256_file(result_path):
            errors.append(f"campaign {label} result hash mismatch")

        raw_runs = result.get("runs")
        runs = (
            [item for item in raw_runs if isinstance(item, Mapping)]
            if isinstance(raw_runs, list)
            else []
        )
        errors.extend(
            f"campaign {label}: {item}"
            for item in validate_run_shape(runs, objective_ids=objective_ids)
        )

        run_ids: list[str] = []
        inspections: list[dict[str, Any]] = []
        run_contracts: list[dict[str, Any]] = []
        for run in runs:
            mode = str(run.get("mode") or "")
            objective_id = str(run.get("objective_id") or "")
            run_id = str(run.get("run_id") or "")
            prefix = f"campaign {label} {mode}/{objective_id}"
            run_ids.append(run_id)
            if run.get("errors") != []:
                errors.append(f"{prefix} contains errors: {run.get('errors')!r}")
            if run.get("orchestration_outcome") not in (None, "completed"):
                errors.append(f"{prefix} orchestration outcome is not completed")

            quality_metrics = _metric_map(run.get("quality_metrics"))
            performance_metrics = _metric_map(run.get("performance_metrics"))
            for name in sorted(MANDATORY_QUALITY_METRICS):
                metric = quality_metrics.get(name)
                if metric is None:
                    errors.append(f"{prefix} lacks quality metric {name}")
                else:
                    errors.extend(
                        f"{prefix}: {item}"
                        for item in validate_metric_record(
                            metric, mode=mode, run_id=run_id, quality=True
                        )
                    )
            for name in sorted(MANDATORY_PERFORMANCE_METRICS):
                metric = performance_metrics.get(name)
                if metric is None:
                    errors.append(f"{prefix} lacks performance metric {name}")
                else:
                    errors.extend(
                        f"{prefix}: {item}"
                        for item in validate_metric_record(
                            metric, mode=mode, run_id=run_id, quality=False
                        )
                    )

            checks = _integrity_map(run.get("integrity_checks"))
            if set(checks) != expected_checks:
                errors.append(f"{prefix} integrity results are incomplete")
            for name, item in checks.items():
                if item.get("passed") is not True:
                    errors.append(f"{prefix} integrity check failed: {name}")

            invariants = _integrity_map(run.get("completeness_invariants"))
            for name, item in invariants.items():
                if item.get("passed") is not True:
                    errors.append(f"{prefix} completeness invariant failed: {name}")

            inspection = inspector.inspect(
                SimpleNamespace(
                    run_id=run_id,
                    mode=mode,
                    objective_id=objective_id,
                )
            )
            inspections.append(inspection)
            inspection_errors = list(inspection.get("errors", []))
            errors.extend(f"{prefix}: {item}" for item in inspection_errors)
            run_contracts.append(
                {
                    "run_id": run_id,
                    "mode": mode,
                    "objective_id": objective_id,
                    "serialized_orchestration_outcome": run.get(
                        "orchestration_outcome"
                    ),
                    "serialized_completeness_invariants": list(invariants),
                    "integrity_checks": sorted(checks),
                    "run_evidence_pass": not inspection_errors,
                }
            )

        if (
            entry.get("runs") != EXPECTED_RUNS_PER_CAMPAIGN
            or entry.get("run_ids") != run_ids
        ):
            errors.append(f"raw manifest campaign {label} run list mismatch")

        errors.extend(
            validate_environment(
                environment,
                campaign_label=label,
                identity=identity,
                tree_hash=tree_hash,
                dataset_hash=dataset_hash,
            )
        )

        campaigns[key] = {
            "campaign_id": result.get("campaign_id"),
            "recommendation": outcome,
            "run_count": len(run_ids),
            "run_ids": run_ids,
            "result_path": str(result_path.relative_to(root)),
            "result_sha256": sha256_file(result_path),
            "environment_path": str(environment_path.relative_to(root)),
            "environment_sha256": sha256_file(environment_path),
            "run_evidence": inspections,
            "run_contracts": run_contracts,
        }
        all_run_ids.extend(run_ids)

    if (
        len(all_run_ids) != EXPECTED_TOTAL_RUNS
        or len(set(all_run_ids)) != EXPECTED_TOTAL_RUNS
    ):
        errors.append("campaigns do not contain 20 globally unique research UUIDs")

    database_runs, database_errors = _database_completion(database_url, all_run_ids)
    errors.extend(database_errors)

    comparison_paths = sorted((root / "reproducibility").glob("*/comparison.json"))
    if not comparison_paths:
        comparison_path = root / "reproducibility" / "missing.json"
        comparison: dict[str, Any] = {}
        errors.append("reproducibility comparison artifact is missing")
    else:
        comparison_path = comparison_paths[-1]
        comparison = load_object(comparison_path)
        errors.extend(validate_reproducibility(comparison))

    raw_reproducibility = raw_manifest.get("reproducibility")
    if not isinstance(raw_reproducibility, Mapping):
        errors.append("raw manifest reproducibility is missing")
    else:
        errors.extend(
            f"raw manifest: {item}"
            for item in validate_reproducibility(raw_reproducibility)
        )

    timing_path = root / "timing-diagnostics.json"
    if timing_path.is_file():
        timing_diagnostics = load_object(timing_path)
        errors.extend(
            validate_timing_diagnostics(
                timing_diagnostics,
                candidate_sha=identity.candidate_sha,
                run_ids=all_run_ids,
            )
        )
    else:
        timing_diagnostics = {}
        errors.append("timing diagnostics artifact is missing")

    recovery_path = root / "recovery-report.txt"
    evidence = {
        "schema_version": "authoritative-release-evidence-v2",
        "candidate_sha": identity.candidate_sha,
        "tree_hash": tree_hash,
        "working_tree_clean": not bool(dirty),
        "dataset_hash": dataset_hash,
        "dataset_version": dataset.get("version"),
        "execution_modes": list(EXPECTED_MODES),
        "agent_led": {"effective": False, "disabled_by_environment": True},
        "objective_ids": objective_ids,
        "expected_runs_per_campaign": EXPECTED_RUNS_PER_CAMPAIGN,
        "expected_total_runs": EXPECTED_TOTAL_RUNS,
        "artifact_retention_days": 90,
        "workflow": {
            "repository": identity.repository,
            "run_id": identity.run_id,
            "run_attempt": identity.run_attempt,
            "workflow_ref": identity.workflow_ref,
            "dispatch_ref": identity.dispatch_ref,
            "dispatch_sha": identity.dispatch_sha,
            "workflow_sha": identity.workflow_sha,
            "checkout_sha": checkout_sha,
            "execution_conclusion": execution_conclusion,
        },
        **campaigns,
        "database_runs": database_runs,
        "reproducibility": {
            "pass": comparison.get("all_within_tolerance") is True,
            "comparison_path": (
                str(comparison_path.relative_to(root))
                if comparison_path.is_file()
                else None
            ),
            "comparison_sha256": (
                sha256_file(comparison_path) if comparison_path.is_file() else None
            ),
            "policy_version": comparison.get("policy_version"),
            "relative_tolerance": comparison.get("relative_tolerance"),
            "operational_ratio_limit": comparison.get("operational_ratio_limit"),
            "observations": comparison.get("observations", []),
        },
        "timing_diagnostics": {
            "path": "timing-diagnostics.json" if timing_path.is_file() else None,
            "sha256": sha256_file(timing_path) if timing_path.is_file() else None,
            "run_count": timing_diagnostics.get("run_count"),
            "reproducibility_failures": timing_diagnostics.get(
                "reproducibility_failures", []
            ),
        },
        "raw_manifest_path": "manifest.json",
        "raw_manifest_sha256": sha256_file(raw_manifest_path),
        "recovery_report": {
            "path": "recovery-report.txt" if recovery_path.is_file() else None,
            "sha256": sha256_file(recovery_path) if recovery_path.is_file() else None,
            "repository_commit_required": False,
        },
        "gate": "PASS" if not errors else "FAIL",
        "errors": errors,
    }
    return evidence, errors


def _failure_manifest(args: argparse.Namespace, error: BaseException) -> dict[str, Any]:
    return {
        "schema_version": "authoritative-release-evidence-v2",
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
        "gate": "FAIL",
        "errors": [f"verifier exception: {type(error).__name__}: {error}"],
        "traceback": traceback.format_exc(),
    }


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
    return parser


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
