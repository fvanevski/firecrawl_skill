"""Strict release benchmark campaign for issue #144.

Strict mode is mandatory. This canonical release/evaluation module owns the
campaign implementation; ``research_store.strict_benchmark`` is compatibility
only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from hashlib import sha256
from pathlib import Path

MODULE_FILE = Path(__file__).resolve()
REPO_ROOT = MODULE_FILE.parents[3]
# Preserve the historical exported SCRIPTS value while using explicit roots
# internally so relocation cannot silently alter path resolution.
SCRIPTS = REPO_ROOT / "scripts" / "research_store" / "strict_benchmark.py"
DEFAULT_DATASET = REPO_ROOT / "tests" / "fixtures" / "benchmark" / "benchmark-v2.json"


def _qdrant_compatibility_errors(caught_warnings) -> tuple[str, ...]:
    """Return only client/server compatibility warnings from Qdrant calls."""
    return tuple(
        str(item.message)
        for item in caught_warnings
        if "incompatible with server version" in str(item.message).lower()
    )


sys.path.insert(0, str(REPO_ROOT / "scripts"))

from .benchmark import (
    RELEASE_MODES,
    MetricStatus,
    ReleaseBenchmarkConfig,
    ReleaseBenchmarkResult,
    ReleaseBenchmarkRunner,
    ReproducibilityComparison,
)
from .workflow import load_benchmark_dataset


def _compute_file_hash(path: Path) -> str:
    h = sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_full_sha(repo: Path | None = None) -> str:
    """Return the current git commit SHA as a full 40-character hex string."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            cwd=str(repo) if repo else None,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "git rev-parse failed")
        sha = result.stdout.strip()
        if len(sha) != 40 or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError(f"expected 40-char hex SHA, got {len(sha)} chars: {sha!r}")
        return sha
    except (RuntimeError, ValueError):
        raise
    except Exception as exc:
        raise ValueError(f"unable to resolve git HEAD: {exc}") from exc


def _get_tree_hash(repo: Path | None = None) -> str:
    """Return the current git tree hash as a full 40-character hex string."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            cwd=str(repo) if repo else None,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "git rev-parse tree failed")
        tree = result.stdout.strip()
        if len(tree) != 40 or not re.fullmatch(r"[0-9a-f]{40}", tree):
            raise ValueError(
                f"expected 40-char hex tree hash, got {len(tree)} chars: {tree!r}"
            )
        return tree
    except (RuntimeError, ValueError):
        raise
    except Exception as exc:
        raise ValueError(f"unable to resolve git tree hash: {exc}") from exc


def _get_firecrawl_version() -> str:
    try:
        import subprocess

        result = subprocess.run(
            ["firecrawl", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:  # noqa: BLE001, S110
        pass
    return "unknown"


def _build_env_manifest(
    candidate_sha: str,
    dataset_path: Path,
    dataset_hash: str,
) -> dict:
    """Build runtime environment metadata for the campaign."""
    import platform

    try:
        tree_hash = _get_tree_hash()
    except ValueError:
        tree_hash = "unresolvable"

    try:
        lock_hash = _compute_file_hash(REPO_ROOT / "requirements-research-store.txt")
    except Exception:  # noqa: BLE001
        lock_hash = "unresolvable"

    secret_url_keys = frozenset(("GENERATIVE_URL", "EMBEDDING_URL", "RERANKER_URL"))
    fingerprints: dict[str, str] = {}
    for key in (
        "GENERATIVE_MODEL",
        "GENERATIVE_URL",
        "EMBEDDING_MODEL",
        "EMBEDDING_URL",
        "EMBEDDING_REVISION",
        "EMBEDDING_DIMENSION",
        "RERANKER_MODEL",
        "RERANKER_URL",
    ):
        val = os.environ.get(key, "")
        if val and key not in secret_url_keys:
            fingerprints[key] = val

    return {
        "candidate_sha": candidate_sha,
        "tree_hash": tree_hash,
        "dataset_path": str(dataset_path),
        "dataset_hash": dataset_hash,
        "dependency_lock_hash": lock_hash,
        "firecrawl_version": _get_firecrawl_version(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        **fingerprints,
    }


def _write_json_atomic(path: Path, data: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(data, indent=2, default=str, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return _compute_file_hash(path)


def _preflight_check(
    database_url: str,
    blob_root: Path | None,
    qdrant_url: str,
    qdrant_api_key: str,
    dataset_path: Path,
    campaign_dir: Path,
    candidate_sha: str,
) -> tuple[bool, list[str]]:
    """Run the complete release preflight through production adapters."""
    from .preflight import run_complete_preflight

    return run_complete_preflight(
        database_url=database_url,
        blob_root=blob_root,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
        dataset_path=dataset_path,
        campaign_dir=campaign_dir,
        candidate_sha=candidate_sha,
        get_full_sha=_get_full_sha,
    )


def _run_campaign(
    campaign_label: str,
    dataset_path: Path,
    database_url: str,
    blob_root: Path | None,
    qdrant_url: str,
    qdrant_api_key: str,
    objective_ids: tuple[str, ...] | None,
    strict: bool,
    reproducibility_tolerance: float,
    campaign_dir: Path,
    candidate_sha: str,
    execution_modes: tuple[str, ...] = ("autonomous_local", "deterministic_debug"),
) -> tuple[ReleaseBenchmarkResult, str]:
    print(f"[Campaign {campaign_label}] Starting strict benchmark campaign...")
    os.environ["CANDIDATE_SHA"] = candidate_sha
    print(f"[Campaign {campaign_label}] Loading dataset from {dataset_path}")
    loader = load_benchmark_dataset(dataset_path)
    config = ReleaseBenchmarkConfig(
        database_url=database_url,
        blob_root=blob_root,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
        execution_modes=execution_modes,
        objective_ids=objective_ids,
        strict=strict,
        reproducibility_tolerance=reproducibility_tolerance,
    )
    print(
        f"[Campaign {campaign_label}] Config: strict={config.strict}, "
        f"modes={config.execution_modes}, "
        f"objectives={config.objective_ids or 'all'}"
    )
    runner = ReleaseBenchmarkRunner(loader, config)
    start = time.monotonic()
    result = runner.run()
    elapsed = (time.monotonic() - start) * 1000
    print(f"[Campaign {campaign_label}] Campaign ID: {result.campaign_id}")
    print(f"[Campaign {campaign_label}] Duration: {elapsed:.0f}ms")
    print(f"[Campaign {campaign_label}] Runs: {len(result.runs)}")
    for run in result.runs:
        status = "OK" if not run.errors else f"ERROR: {run.errors}"
        print(
            f"  - {run.mode}: run_id={run.run_id[:12] if run.run_id else 'N/A'} ... {status}"
        )
    if result.recommendation:
        print(
            f"[Campaign {campaign_label}] Recommendation: "
            f"{result.recommendation.outcome}"
        )

    campaign_ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    artifacts_dir = campaign_dir / campaign_label / campaign_ts
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    result_path = artifacts_dir / "result.json"
    result_hash = _write_json_atomic(
        result_path,
        {
            "schema_version": result.schema_version,
            "campaign_id": result.campaign_id,
            "campaign_timestamp": result.campaign_timestamp,
            "environment": result.environment,
            "recommendation": {
                "outcome": result.recommendation.outcome if result.recommendation else None,
                "supported_claims": result.recommendation.supported_claims
                if result.recommendation
                else (),
                "withdrawn_claims": result.recommendation.withdrawn_claims
                if result.recommendation
                else (),
                "known_limitations": result.recommendation.known_limitations
                if result.recommendation
                else (),
                "conditions": result.recommendation.conditions
                if result.recommendation
                else (),
                "p0_regressions": result.recommendation.p0_regressions
                if result.recommendation
                else (),
            }
            if result.recommendation
            else None,
            "total_duration_ms": result.total_duration_ms,
            "runs": [
                {
                    "campaign_id": run.campaign_id,
                    "run_id": run.run_id,
                    "mode": run.mode,
                    "objective_id": run.objective_id,
                    "quality": {
                        "candidate_recall": run.quality.candidate_recall
                        if run.quality
                        else None,
                        "source_quality_score": run.quality.source_quality_score
                        if run.quality
                        else None,
                        "coverage_completeness": run.quality.coverage_completeness
                        if run.quality
                        else None,
                        "unsupported_claim_rate": run.quality.unsupported_claim_rate
                        if run.quality
                        else None,
                        "citation_accuracy": run.quality.citation_accuracy
                        if run.quality
                        else None,
                        "report_quality_score": run.quality.report_quality_score
                        if run.quality
                        else None,
                    }
                    if run.quality
                    else None,
                    "quality_metrics": [
                        {
                            "name": qm.name,
                            "value": qm.value,
                            "status": getattr(qm, "status", MetricStatus.UNEVALUATED).value,
                            "formula": qm.formula,
                            "source": {
                                "table": qm.source.table,
                                "column": qm.source.column,
                                "run_id": qm.source.run_id,
                                "method": qm.source.method,
                                "record_ids": list(qm.source.event_ids),
                                "stages": list(qm.source.stages),
                                "stage_set_version": qm.source.stage_set_version,
                                "sample_count": qm.source.sample_count,
                                "device_type": qm.source.device_type,
                                "device_index": qm.source.device_index,
                                "device_uuid": qm.source.device_uuid,
                                "collector": qm.source.collector,
                                "collector_version": qm.source.collector_version,
                                "status_counts": dict(qm.source.status_counts),
                            },
                        }
                        for qm in run.quality_metrics
                    ]
                    if run.quality_metrics
                    else [],
                    "performance": {
                        "total_latency_ms": run.performance.total_latency_ms
                        if run.performance
                        else None,
                        "total_tokens": run.performance.total_tokens
                        if run.performance
                        else None,
                        "semantic_calls": run.performance.semantic_calls
                        if run.performance
                        else None,
                        "cache_hit_rate": run.performance.cache_hit_rate
                        if run.performance
                        else None,
                        "embedding_throughput": run.performance.embedding_throughput
                        if run.performance
                        else None,
                        "cpu_percent": run.performance.cpu_percent
                        if run.performance
                        else None,
                        "gpu_memory_mb": run.performance.gpu_memory_mb
                        if run.performance
                        else None,
                    }
                    if run.performance
                    else None,
                    "performance_metrics": [
                        {
                            "name": pm.name,
                            "value": pm.value,
                            "status": getattr(pm, "status", MetricStatus.UNEVALUATED).value,
                            "formula": pm.formula,
                            "source": {
                                "table": pm.source.table,
                                "column": pm.source.column,
                                "run_id": pm.source.run_id,
                                "method": pm.source.method,
                                "record_ids": list(pm.source.event_ids),
                                "stages": list(pm.source.stages),
                                "stage_set_version": pm.source.stage_set_version,
                                "sample_count": pm.source.sample_count,
                                "device_type": pm.source.device_type,
                                "device_index": pm.source.device_index,
                                "device_uuid": pm.source.device_uuid,
                                "collector": pm.source.collector,
                                "collector_version": pm.source.collector_version,
                                "status_counts": dict(pm.source.status_counts),
                            },
                        }
                        for pm in run.performance_metrics
                    ]
                    if run.performance_metrics
                    else [],
                    "errors": run.errors,
                    "integrity_checks": [
                        {
                            "check": check.check_name,
                            "passed": check.passed,
                            "details": check.details,
                        }
                        for check in run.integrity_checks
                    ]
                    if run.integrity_checks
                    else [],
                }
                for run in result.runs
            ],
        },
    )

    dataset_hash = _compute_file_hash(dataset_path)
    env_manifest = {
        **_build_env_manifest(candidate_sha, dataset_path, dataset_hash),
        "database_url_set": bool(database_url),
        "blob_root_set": bool(blob_root),
        "strict": strict,
        "execution_modes": RELEASE_MODES,
        "objective_ids": list(objective_ids) if objective_ids else ["all"],
        "reproducibility_tolerance": reproducibility_tolerance,
        "reproducibility_policy_version": "reproducibility-policy-v2",
        "operational_reproducibility_ratio_limit": float(
            loader.quality_thresholds.get(
                "max_operational_reproducibility_ratio",
                config.operational_reproducibility_ratio_limit,
            )
        ),
    }
    _write_json_atomic(artifacts_dir / "environment.json", env_manifest)
    (artifacts_dir / "summary.txt").write_text(
        result.summary() + "\n", encoding="utf-8"
    )
    print(f"[Campaign {campaign_label}] Artifacts written to {artifacts_dir}")
    print(f"[Campaign {campaign_label}] Result hash: {result_hash}")
    return result, result_hash


def _compare_campaigns(
    result_a: ReleaseBenchmarkResult,
    result_b: ReleaseBenchmarkResult,
    campaign_dir: Path,
    reproducibility_tolerance: float,
    dataset_path: Path,
) -> ReproducibilityComparison:
    print("[Reproducibility] Comparing Campaign A and Campaign B...")
    loader = load_benchmark_dataset(dataset_path)
    runner = ReleaseBenchmarkRunner(loader, ReleaseBenchmarkConfig())
    comparison = runner.compare_campaigns(
        result_a, result_b, tolerance=reproducibility_tolerance
    )
    print(f"[Reproducibility] All within tolerance: {comparison.all_within_tolerance}")
    for detail in comparison.details:
        print(f"  - {detail}")
    comparison_dir = (
        campaign_dir
        / "reproducibility"
        / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    )
    comparison_dir.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(
        comparison_dir / "comparison.json",
        {
            "schema_version": comparison.schema_version,
            "run_a_id": comparison.run_a_id,
            "run_b_id": comparison.run_b_id,
            "mode": comparison.mode,
            "objective_id": comparison.objective_id,
            "all_within_tolerance": comparison.all_within_tolerance,
            "quality_tolerances": list(comparison.quality_tolerances),
            "performance_tolerances": list(comparison.performance_tolerances),
            "policy_version": comparison.policy_version,
            "relative_tolerance": comparison.relative_tolerance,
            "operational_ratio_limit": comparison.operational_ratio_limit,
            "operational_absolute_tolerances": dict(
                comparison.operational_absolute_tolerances
            ),
            "details": comparison.details,
            "observations": comparison.observations,
        },
    )
    lines = [
        f"Reproducibility Comparison — {comparison.run_a_id} vs {comparison.run_b_id}",
        f"Outcome: {'PASS' if comparison.all_within_tolerance else 'FAIL'}",
        f"Quality tolerances: {len(comparison.quality_tolerances)} metrics compared",
        f"Performance tolerances: {len(comparison.performance_tolerances)} metrics compared",
    ]
    lines.extend(f"  - {detail}" for detail in comparison.details)
    lines.extend(f"  - observation: {item}" for item in comparison.observations)
    (comparison_dir / "summary.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return comparison


def _build_manifest(
    campaign_dir: Path,
    result_a: ReleaseBenchmarkResult,
    result_b: ReleaseBenchmarkResult,
    comparison: ReproducibilityComparison,
    dataset_path: Path,
    candidate_sha: str,
) -> dict:
    campaign_a_dir = None
    campaign_b_dir = None
    for label_dir in (campaign_dir / "A", campaign_dir / "B"):
        if label_dir.exists():
            latest = max(label_dir.iterdir(), key=lambda path: path.name)
            if label_dir == campaign_dir / "A":
                campaign_a_dir = latest
            else:
                campaign_b_dir = latest
    try:
        tree_hash = _get_tree_hash()
    except ValueError:
        tree_hash = "unresolvable"
    return {
        "schema_version": "campaign-manifest-v1",
        "candidate_sha": candidate_sha,
        "tree_hash": tree_hash,
        "dataset_path": str(dataset_path),
        "dataset_hash": _compute_file_hash(dataset_path),
        "dataset_version": load_benchmark_dataset(dataset_path).dataset.version,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "campaign_a": {
            "campaign_id": result_a.campaign_id,
            "result_hash": _compute_file_hash(campaign_a_dir / "result.json")
            if campaign_a_dir
            else None,
            "result_path": str(campaign_a_dir) if campaign_a_dir else None,
            "runs": len(result_a.runs),
            "run_ids": [run.run_id for run in result_a.runs],
            "recommendation": result_a.recommendation.outcome
            if result_a.recommendation
            else None,
        },
        "campaign_b": {
            "campaign_id": result_b.campaign_id,
            "result_hash": _compute_file_hash(campaign_b_dir / "result.json")
            if campaign_b_dir
            else None,
            "result_path": str(campaign_b_dir) if campaign_b_dir else None,
            "runs": len(result_b.runs),
            "run_ids": [run.run_id for run in result_b.runs],
            "recommendation": result_b.recommendation.outcome
            if result_b.recommendation
            else None,
        },
        "reproducibility": {
            "all_within_tolerance": comparison.all_within_tolerance,
            "run_a_id": comparison.run_a_id,
            "run_b_id": comparison.run_b_id,
            "policy_version": comparison.policy_version,
            "relative_tolerance": comparison.relative_tolerance,
            "operational_ratio_limit": comparison.operational_ratio_limit,
            "operational_absolute_tolerances": dict(
                comparison.operational_absolute_tolerances
            ),
            "details": list(comparison.details),
            "observations": list(comparison.observations),
        },
        "modes": list(RELEASE_MODES),
    }


def main(
    argv: list[str] | None = None,
    execution_modes: tuple[str, ...] = ("autonomous_local", "deterministic_debug"),
) -> int:
    parser = argparse.ArgumentParser(
        description="Strict release benchmark campaign (issue #144). "
        "Strict mode is mandatory and cannot be disabled."
    )
    parser.add_argument("--candidate-sha", type=str, required=True)
    parser.add_argument(
        "--campaign-dir",
        type=Path,
        default=Path("/tmp/firecrawl_strict_campaign"),
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--database-url", type=str, default=os.environ.get("DATABASE_URL", "")
    )
    parser.add_argument(
        "--blob-root",
        type=Path,
        default=Path(os.environ.get("BLOB_ROOT", "/tmp/benchmark-blobs")),
    )
    parser.add_argument(
        "--qdrant-url", type=str, default=os.environ.get("QDRANT_URL", "")
    )
    parser.add_argument(
        "--qdrant-api-key", type=str, default=os.environ.get("QDRANT_API_KEY", "")
    )
    parser.add_argument("--objectives", type=str, default=None)
    parser.add_argument("--tolerance", type=float, default=0.15)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--recovery-report", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", default=False)
    args = parser.parse_args(argv)

    strict = True
    candidate_sha = args.candidate_sha
    if len(candidate_sha) != 40 or not re.fullmatch(r"[0-9a-f]{40}", candidate_sha):
        print(
            f"ERROR: --candidate-sha must be a full 40-character hex string; "
            f"got {len(candidate_sha)} chars: {candidate_sha!r}",
            file=sys.stderr,
        )
        return 1
    if not args.dataset.is_file():
        print(f"ERROR: dataset not found: {args.dataset}", file=sys.stderr)
        return 1
    if not args.database_url:
        print(
            "ERROR: --database-url is required (or DATABASE_URL env var)",
            file=sys.stderr,
        )
        return 1
    if args.tolerance < 0.0 or args.tolerance > 1.0:
        print("ERROR: --tolerance must be between 0.0 and 1.0", file=sys.stderr)
        return 1

    objective_ids = (
        tuple(item.strip() for item in args.objectives.split(","))
        if args.objectives
        else None
    )
    campaign_dir = args.campaign_dir
    campaign_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Strict Release Benchmark Campaign (issue #144)")
    print("=" * 60)
    print("  Strict mode:         ON (mandatory)")
    print(f"  Candidate SHA:       {candidate_sha}")
    print(f"  Dataset:             {args.dataset}")
    print(f"  Database URL:        {'set' if args.database_url else 'NOT SET'}")
    print(f"  Blob root:           {args.blob_root}")
    print(f"  Qdrant URL:          {args.qdrant_url or 'NOT SET'}")
    print(f"  Objectives:          {list(objective_ids) if objective_ids else 'all'}")
    print(f"  Reproducibility tolerance: {args.tolerance}")
    print("=" * 60)

    if args.dry_run:
        print("\n[Dry run] Configuration validated. No campaigns executed.")
        ok, errors = _preflight_check(
            database_url=args.database_url,
            blob_root=args.blob_root,
            qdrant_url=args.qdrant_url,
            qdrant_api_key=args.qdrant_api_key,
            dataset_path=args.dataset,
            campaign_dir=campaign_dir,
            candidate_sha=candidate_sha,
        )
        if not ok:
            print("\n[Preflight] FAILED — required infrastructure unavailable:")
            for error in errors:
                print(f"  - {error}")
            return 1
        return 0

    print("\n[Preflight] Checking required infrastructure...")
    ok, errors = _preflight_check(
        database_url=args.database_url,
        blob_root=args.blob_root,
        qdrant_url=args.qdrant_url,
        qdrant_api_key=args.qdrant_api_key,
        dataset_path=args.dataset,
        campaign_dir=campaign_dir,
        candidate_sha=candidate_sha,
    )
    if not ok:
        print("\n[Preflight] FAILED — required infrastructure unavailable:")
        for error in errors:
            print(f"  - {error}")
        print("\nCampaign execution aborted. Fix the above issues and retry.")
        return 1
    print("[Preflight] OK — all required infrastructure available.\n")

    result_a, hash_a = _run_campaign(
        campaign_label="A",
        dataset_path=args.dataset,
        database_url=args.database_url,
        blob_root=args.blob_root,
        qdrant_url=args.qdrant_url,
        qdrant_api_key=args.qdrant_api_key,
        objective_ids=objective_ids,
        strict=strict,
        reproducibility_tolerance=args.tolerance,
        campaign_dir=campaign_dir,
        candidate_sha=candidate_sha,
        execution_modes=execution_modes,
    )
    result_b, hash_b = _run_campaign(
        campaign_label="B",
        dataset_path=args.dataset,
        database_url=args.database_url,
        blob_root=args.blob_root,
        qdrant_url=args.qdrant_url,
        qdrant_api_key=args.qdrant_api_key,
        objective_ids=objective_ids,
        strict=strict,
        reproducibility_tolerance=args.tolerance,
        campaign_dir=campaign_dir,
        candidate_sha=candidate_sha,
        execution_modes=execution_modes,
    )
    comparison = _compare_campaigns(
        result_a, result_b, campaign_dir, args.tolerance, args.dataset
    )
    manifest = _build_manifest(
        campaign_dir, result_a, result_b, comparison, args.dataset, candidate_sha
    )
    manifest_path = args.manifest or campaign_dir / "manifest.json"
    _write_json_atomic(manifest_path, manifest)

    print("\n" + "=" * 60)
    print("Campaign Summary")
    print("=" * 60)
    print(f"  Candidate SHA:       {candidate_sha}")
    print(f"  Campaign A: {result_a.campaign_id} (hash: {hash_a[:12]})")
    print(f"  Campaign B: {result_b.campaign_id} (hash: {hash_b[:12]})")
    print(f"  Reproducibility: {'PASS' if comparison.all_within_tolerance else 'FAIL'}")
    print(f"  Manifest: {manifest_path}")
    if result_a.recommendation:
        print(f"  Campaign A recommendation: {result_a.recommendation.outcome}")
    if result_b.recommendation:
        print(f"  Campaign B recommendation: {result_b.recommendation.outcome}")

    recovery_report_path = args.recovery_report or campaign_dir / "recovery-report.txt"
    recovery_lines = [
        "Recovery Report — Strict Benchmark Campaign",
        f"Candidate SHA: {candidate_sha}",
        f"Timestamp: {time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime())}",
        f"Campaign A: {result_a.campaign_id}",
        f"Campaign B: {result_b.campaign_id}",
        f"Reproducibility: {'PASS' if comparison.all_within_tolerance else 'FAIL'}",
        "Campaign A run IDs:",
        *[
            f"- {run.mode}/{run.objective_id}: {run.run_id or 'MISSING'}"
            for run in result_a.runs
        ],
        "Campaign B run IDs:",
        *[
            f"- {run.mode}/{run.objective_id}: {run.run_id or 'MISSING'}"
            for run in result_b.runs
        ],
    ]
    if result_a.recommendation:
        recovery_lines.append(
            f"Campaign A recommendation: {result_a.recommendation.outcome}"
        )
    if result_b.recommendation:
        recovery_lines.append(
            f"Campaign B recommendation: {result_b.recommendation.outcome}"
        )
    recovery_lines.append("")
    recovery_report_path.parent.mkdir(parents=True, exist_ok=True)
    recovery_report_path.write_text("\n".join(recovery_lines) + "\n", encoding="utf-8")
    manifest["recovery_report"] = {
        "path": str(recovery_report_path),
        "sha256": _compute_file_hash(recovery_report_path),
    }
    _write_json_atomic(manifest_path, manifest)
    print(f"  Recovery report: {recovery_report_path}")
    print(f"  Manifest hash: {_compute_file_hash(manifest_path)[:12]}")
    print("=" * 60)

    def is_go(rec) -> bool:
        return bool(rec and rec.outcome == "go")

    if not is_go(result_a.recommendation) or not is_go(result_b.recommendation):
        print(
            "\nFATAL: Release policy not met. "
            "(Must be unequivocally GO with reproducibility passing)"
        )
        return 1
    if not comparison.all_within_tolerance:
        print(
            "\nWARNING: Reproducibility comparison FAILED. "
            "Out-of-tolerance differences detected."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
