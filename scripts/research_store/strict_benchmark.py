"""Strict release benchmark campaign for issue #144.

This module provides a CLI entry point that enforces strict release-mode
benchmark execution: two complete campaigns (A and B) with identical
versioned inputs, reproducibility comparison, and durable artifact
manifests.

Strict mode is mandatory and cannot be disabled through ordinary flags.
Simulation and workflow substitution are impossible in release campaigns.

Usage:
    strict_benchmark [--campaign-dir DIR] [--dataset PATH] [--database-url URL]
                     [--blob-root PATH] [--qdrant-url URL] [--qdrant-api-key KEY]
                     [--objectives OBJ1,OBJ2,...] [--tolerance FLOAT]
                     [--manifest PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from hashlib import sha256
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.release_benchmark import (
    RELEASE_MODES,
    MetricStatus,
    ReleaseBenchmarkConfig,
    ReleaseBenchmarkResult,
    ReleaseBenchmarkRunner,
    ReproducibilityComparison,
)
from research_store.workflow_benchmark import load_benchmark_dataset


def _compute_file_hash(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_env_manifest() -> dict:
    """Build runtime environment metadata for the campaign."""
    import platform

    return {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "commit": _get_commit_sha(),
    }


def _get_commit_sha() -> str:
    """Return the current git commit SHA, or 'unknown'."""
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()[:12]
    except Exception:  # noqa: BLE001
        return "unknown"
    return "unknown"


def _write_json_atomic(path: Path, data: object) -> str:
    """Write JSON atomically via temp-file rename. Returns file hash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(data, indent=2, default=str, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return _compute_file_hash(path)


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
) -> tuple[ReleaseBenchmarkResult, str]:
    """Execute a single strict campaign.

    Args:
        campaign_label: Human-readable label (e.g. "A" or "B").
        dataset_path: Path to the benchmark dataset JSON file.
        database_url: PostgreSQL connection string.
        blob_root: Path to the content-addressed blob store root.
        qdrant_url: Qdrant URL.
        qdrant_api_key: Qdrant API key.
        objective_ids: Specific objective IDs to run (None = all).
        strict: Whether strict mode is enabled.
        reproducibility_tolerance: Tolerance for reproducibility comparison.
        campaign_dir: Directory to write campaign artifacts to.

    Returns:
        Tuple of (result, campaign_id).
    """
    print(f"[Campaign {campaign_label}] Starting strict benchmark campaign...")

    # Load benchmark dataset
    print(f"[Campaign {campaign_label}] Loading dataset from {dataset_path}")
    loader = load_benchmark_dataset(dataset_path)

    # Build config with strict mode mandatory
    config = ReleaseBenchmarkConfig(
        database_url=database_url,
        blob_root=blob_root,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
        execution_modes=RELEASE_MODES,
        objective_ids=objective_ids,
        strict=strict,
        reproducibility_tolerance=reproducibility_tolerance,
    )

    print(
        f"[Campaign {campaign_label}] Config: strict={config.strict}, "
        f"modes={config.execution_modes}, "
        f"objectives={config.objective_ids or 'all'}"
    )

    # Build runner
    runner = ReleaseBenchmarkRunner(loader, config)

    # Execute campaign
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

    # Persist campaign artifacts
    campaign_ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    artifacts_dir = campaign_dir / campaign_label / campaign_ts
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Write result JSON
    result_path = artifacts_dir / "result.json"
    result_hash = _write_json_atomic(
        result_path,
        {
            "schema_version": result.schema_version,
            "campaign_id": result.campaign_id,
            "campaign_timestamp": result.campaign_timestamp,
            "environment": result.environment,
            "recommendation": {
                "outcome": result.recommendation.outcome
                if result.recommendation
                else None,
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
                            "status": getattr(
                                qm, "status", MetricStatus.UNEVALUATED
                            ).value,
                            "formula": qm.formula,
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
                            "status": getattr(
                                pm, "status", MetricStatus.UNEVALUATED
                            ).value,
                            "formula": pm.formula,
                        }
                        for pm in run.performance_metrics
                    ]
                    if run.performance_metrics
                    else [],
                    "errors": run.errors,
                    "integrity_checks": [
                        {
                            "check": c.check_name,
                            "passed": c.passed,
                            "details": c.details,
                        }
                        for c in run.integrity_checks
                    ]
                    if run.integrity_checks
                    else [],
                }
                for run in result.runs
            ],
        },
    )

    # Write environment manifest
    env_manifest = {
        **_build_env_manifest(),
        "dataset_path": str(dataset_path),
        "dataset_hash": _compute_file_hash(dataset_path),
        "database_url_set": bool(database_url),
        "blob_root_set": bool(blob_root),
        "strict": strict,
        "execution_modes": RELEASE_MODES,
        "objective_ids": list(objective_ids) if objective_ids else ["all"],
        "reproducibility_tolerance": reproducibility_tolerance,
    }
    env_manifest_path = artifacts_dir / "environment.json"
    _write_json_atomic(env_manifest_path, env_manifest)

    # Write summary
    summary_path = artifacts_dir / "summary.txt"
    summary_path.write_text(result.summary() + "\n", encoding="utf-8")

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
    """Compare two campaign runs for reproducibility.

    Args:
        result_a: Campaign A result.
        result_b: Campaign B result.
        campaign_dir: Directory to write comparison artifacts to.
        reproducibility_tolerance: Tolerance to use for the comparison.
        dataset_path: Path to the benchmark dataset used by both campaigns.

    Returns:
        ReproducibilityComparison.
    """
    print("[Reproducibility] Comparing Campaign A and Campaign B...")

    loader = load_benchmark_dataset(dataset_path)
    runner = ReleaseBenchmarkRunner(loader, ReleaseBenchmarkConfig())

    comparison = runner.compare_campaigns(
        result_a, result_b, tolerance=reproducibility_tolerance
    )

    print(f"[Reproducibility] All within tolerance: {comparison.all_within_tolerance}")
    for detail in comparison.details:
        print(f"  - {detail}")

    # Write comparison artifacts
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
            "details": comparison.details,
        },
    )

    summary_path = comparison_dir / "summary.txt"
    lines = [
        f"Reproducibility Comparison — {comparison.run_a_id} vs {comparison.run_b_id}",
        f"Outcome: {'PASS' if comparison.all_within_tolerance else 'FAIL'}",
        f"Quality tolerances: {len(comparison.quality_tolerances)} metrics compared",
        f"Performance tolerances: {len(comparison.performance_tolerances)} metrics compared",
    ]
    for detail in comparison.details:
        lines.append(f"  - {detail}")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return comparison


def _build_manifest(
    campaign_dir: Path,
    result_a: ReleaseBenchmarkResult,
    result_b: ReleaseBenchmarkResult,
    comparison: ReproducibilityComparison,
    dataset_path: Path,
) -> dict:
    """Build the durable artifact manifest."""
    campaign_a_dir = None
    campaign_b_dir = None
    for label_dir in (campaign_dir / "A", campaign_dir / "B"):
        if label_dir.exists():
            latest = max(label_dir.iterdir(), key=lambda p: p.name)
            if label_dir == campaign_dir / "A":
                campaign_a_dir = latest
            else:
                campaign_b_dir = latest

    manifest = {
        "schema_version": "campaign-manifest-v1",
        "dataset_path": str(dataset_path),
        "dataset_hash": _compute_file_hash(dataset_path),
        "dataset_version": "benchmark-v1",
        "commit": _get_commit_sha(),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "campaign_a": {
            "campaign_id": result_a.campaign_id,
            "result_hash": _compute_file_hash(campaign_a_dir / "result.json")
            if campaign_a_dir
            else None,
            "result_path": str(campaign_a_dir) if campaign_a_dir else None,
            "runs": len(result_a.runs),
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
            "recommendation": result_b.recommendation.outcome
            if result_b.recommendation
            else None,
        },
        "reproducibility": {
            "all_within_tolerance": comparison.all_within_tolerance,
            "run_a_id": comparison.run_a_id,
            "run_b_id": comparison.run_b_id,
            "details": list(comparison.details),
        },
        "modes": list(RELEASE_MODES),
    }
    return manifest


def main(argv: list[str] | None = None) -> int:
    """Execute strict release benchmark campaigns.

    Returns 0 on success, 1 on failure.
    """
    parser = argparse.ArgumentParser(
        description="Strict release benchmark campaign (issue #144). "
        "Strict mode is mandatory and cannot be disabled."
    )
    parser.add_argument(
        "--campaign-dir",
        type=Path,
        default=Path("/tmp/firecrawl_strict_campaign"),
        help="Directory to write campaign artifacts "
        "(default: /tmp/firecrawl_strict_campaign)",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=SCRIPTS.parent.parent
        / "tests"
        / "fixtures"
        / "benchmark"
        / "benchmark-v1.json",
        help="Path to the benchmark dataset JSON file",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=os.environ.get("DATABASE_URL", ""),
        help="PostgreSQL connection string",
    )
    parser.add_argument(
        "--blob-root",
        type=Path,
        default=Path(os.environ.get("BLOB_ROOT", "/tmp/benchmark-blobs")),
        help="Path to the content-addressed blob store root",
    )
    parser.add_argument(
        "--qdrant-url",
        type=str,
        default=os.environ.get("QDRANT_URL", ""),
        help="Qdrant URL",
    )
    parser.add_argument(
        "--qdrant-api-key",
        type=str,
        default=os.environ.get("QDRANT_API_KEY", ""),
        help="Qdrant API key",
    )
    parser.add_argument(
        "--objectives",
        type=str,
        default=None,
        help="Comma-separated objective IDs to run (default: all)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.15,
        help="Reproducibility tolerance (default: 0.15)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to write the final manifest (default: <campaign-dir>/manifest.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Validate configuration without executing campaigns",
    )

    args = parser.parse_args(argv)

    # ── Strict mode is mandatory ─────────────────────────────────────────
    # There is no --no-strict flag. Strict mode is always ON for release
    # campaigns per issue #144.
    strict = True

    # ── Validate inputs ──────────────────────────────────────────────────
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
        print(
            "ERROR: --tolerance must be between 0.0 and 1.0",
            file=sys.stderr,
        )
        return 1

    # Parse objectives
    objective_ids: tuple[str, ...] | None = None
    if args.objectives:
        objective_ids = tuple(o.strip() for o in args.objectives.split(","))

    campaign_dir = args.campaign_dir
    campaign_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Strict Release Benchmark Campaign (issue #144)")
    print("=" * 60)
    print("  Strict mode:         ON (mandatory)")
    print(f"  Dataset:             {args.dataset}")
    print(f"  Database URL:        {'set' if args.database_url else 'NOT SET'}")
    print(f"  Blob root:           {args.blob_root}")
    print(f"  Qdrant URL:          {args.qdrant_url or 'NOT SET'}")
    print(f"  Objectives:          {list(objective_ids) if objective_ids else 'all'}")
    print(f"  Reproducibility tolerance: {args.tolerance}")
    print(f"  Commit:              {_get_commit_sha()}")
    print("=" * 60)

    if args.dry_run:
        print("\n[Dry run] Configuration validated. No campaigns executed.")
        return 0

    # ── Execute Campaign A ───────────────────────────────────────────────
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
    )

    # ── Execute Campaign B ───────────────────────────────────────────────
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
    )

    # ── Reproducibility comparison ───────────────────────────────────────
    comparison = _compare_campaigns(
        result_a, result_b, campaign_dir, args.tolerance, args.dataset
    )

    # ── Build and write manifest ─────────────────────────────────────────
    manifest = _build_manifest(
        campaign_dir, result_a, result_b, comparison, args.dataset
    )
    manifest_path = args.manifest or campaign_dir / "manifest.json"
    _write_json_atomic(manifest_path, manifest)

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Campaign Summary")
    print("=" * 60)
    print(f"  Campaign A: {result_a.campaign_id} (hash: {hash_a[:12]})")
    print(f"  Campaign B: {result_b.campaign_id} (hash: {hash_b[:12]})")
    print(f"  Reproducibility: {'PASS' if comparison.all_within_tolerance else 'FAIL'}")
    print(f"  Manifest: {manifest_path}")
    print(f"  Manifest hash: {_compute_file_hash(manifest_path)[:12]}")

    if result_a.recommendation:
        print(f"  Campaign A recommendation: {result_a.recommendation.outcome}")
    if result_b.recommendation:
        print(f"  Campaign B recommendation: {result_b.recommendation.outcome}")

    # ── Recovery report ──────────────────────────────────────────────────
    # Generate a recovery report at the repository root for release
    # evidence artifact collection (issue #145).
    repo_root = SCRIPTS.parent.parent
    recovery_report_path = repo_root / "recovery-report.txt"
    recovery_lines = [
        "Recovery Report — Strict Benchmark Campaign",
        f"Commit: {_get_commit_sha()}",
        f"Timestamp: {time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime())}",
        f"Campaign A: {result_a.campaign_id}",
        f"Campaign B: {result_b.campaign_id}",
        f"Reproducibility: {'PASS' if comparison.all_within_tolerance else 'FAIL'}",
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
    recovery_report_path.write_text("\n".join(recovery_lines) + "\n", encoding="utf-8")
    print(f"  Recovery report: {recovery_report_path}")

    print("=" * 60)

    # Exit with failure if any campaign failed, reproducibility failed,
    # or either campaign recommends NO_GO.
    no_go_a = (
        result_a.recommendation.outcome == "no_go" if result_a.recommendation else False
    )
    no_go_b = (
        result_b.recommendation.outcome == "no_go" if result_b.recommendation else False
    )

    if no_go_a or no_go_b:
        if no_go_a and no_go_b:
            print("\nFATAL: Both campaigns recommend NO_GO. Release is rejected.")
        elif no_go_a:
            print("\nFATAL: Campaign A recommends NO_GO. Release is rejected.")
        else:
            print("\nFATAL: Campaign B recommends NO_GO. Release is rejected.")
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
