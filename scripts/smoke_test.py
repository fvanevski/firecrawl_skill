#!/usr/bin/env python3
"""Reduced real smoke test for the authoritative extraction pipeline.

Runs all three execution modes against one benchmark objective with the
complete real service stack.  Two repetitions are executed and compared
for reproducibility.

This smoke test is NOT closure evidence.  Its purpose is to validate:
- distinct mode authority (agent_led, autonomous_local, deterministic_debug)
- substantive extracted assets and reports
- metric completeness
- artifact serialization
- reproducibility mechanics
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

# Ensure the research-store venv site-packages is on the path.
RESEARCH_VENV_SITE_PACKAGES = (
    Path(__file__).resolve().parent.parent
    / ".venv-research-store"
    / "lib"
    / "python3.12"
    / "site-packages"
)
if RESEARCH_VENV_SITE_PACKAGES.is_dir():
    sys.path.insert(0, str(RESEARCH_VENV_SITE_PACKAGES))

# Ensure scripts/ is on the path so relative imports resolve.
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import model_gateway
from research_store.release_benchmark import (
    RELEASE_MODES,
    ReleaseBenchmarkConfig,
    ReleaseBenchmarkRunner,
)
from research_store.workflow_benchmark import load_benchmark_dataset

BENCHMARK_FIXTURE = (
    SCRIPTS.parent / "tests" / "fixtures" / "benchmark" / "benchmark-v1.json"
)


# ---------------------------------------------------------------------------
# Minimal host artifact supplier for smoke testing agent_led mode
# ---------------------------------------------------------------------------


class SmokeHostArtifactSupplier:
    """Supply host-authored artifacts via the local model gateway.

    In production, agent_led requires a genuine external agent (human,
    remote model, or separate process).  For smoke testing we use the
    local gateway so the pipeline exercises the full ingestion path
    without requiring an external host.
    """

    def supply(
        self,
        *,
        semantic_context: dict[str, Any],
        schema: Mapping[str, Any],
        provider: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        prompt_version: str,
        **call_kwargs: Any,
    ) -> model_gateway.StructuredResult:
        try:
            result = model_gateway.call_structured(
                provider=provider,
                model=model,
                schema=schema,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_version=prompt_version,
                semantic_persistence=None,  # host-supplied, not persisted by model
                semantic_context=semantic_context,
            )
            return model_gateway.StructuredResult(
                value=result.value,
                provenance={
                    "supplier": "smoke_host_agent",
                    "call_id": str(uuid4()),
                    "timestamp": time.strftime(
                        "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()
                    ),
                    "usage": result.provenance.get("usage", {}),
                },
                attempts=(result,),
                error=result.error or "",
            )
        except RuntimeError as exc:
            return model_gateway.StructuredResult(
                value=None,
                provenance={"supplier": "smoke_host_agent", "error": str(exc)},
                attempts=(),
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Campaign execution
# ---------------------------------------------------------------------------


def run_single_campaign(
    campaign_label: str,
    candidate_sha: str,
    database_url: str,
    blob_root: Path,
    qdrant_url: str,
    qdrant_api_key: str,
) -> dict:
    """Execute one campaign across all three modes and return the result as dict."""
    print(f"\n{'=' * 60}")
    print(f"[Smoke Test] Campaign: {campaign_label}")
    print(f"[Smoke Test] Candidate SHA: {candidate_sha}")
    print(f"[Smoke Test] Modes: {RELEASE_MODES}")
    print(f"{'=' * 60}")

    # Load benchmark dataset, single objective (obj-001)
    loader = load_benchmark_dataset(BENCHMARK_FIXTURE)
    objectives = [obj for obj in loader.objectives if obj.id == "obj-001"]
    if not objectives:
        print("[ERROR] obj-001 not found in benchmark dataset", file=sys.stderr)
        sys.exit(1)
    loader.objectives = objectives
    print(f"[Smoke Test] Objective: {objectives[0].id} - {objectives[0].title}")

    # Host artifact supplier for agent_led mode
    host_supplier = SmokeHostArtifactSupplier()

    config = ReleaseBenchmarkConfig(
        database_url=database_url,
        blob_root=blob_root,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
        execution_modes=RELEASE_MODES,
        objective_ids=("obj-001",),
        strict=True,
        reproducibility_tolerance=0.15,
        host_artifact_supplier=host_supplier,
    )

    print(
        f"[Smoke Test] Config: strict={config.strict}, "
        f"modes={config.execution_modes}, "
        f"host_supplier={config.host_artifact_supplier is not None}"
    )

    runner = ReleaseBenchmarkRunner(loader, config)

    start = time.monotonic()
    result = runner.run()
    elapsed = (time.monotonic() - start) * 1000

    print(f"\n[Smoke Test] Campaign ID: {result.campaign_id}")
    print(f"[Smoke Test] Duration: {elapsed:.0f}ms")
    print(f"[Smoke Test] Runs: {len(result.runs)}")

    for run in result.runs:
        status = "OK" if not run.errors else f"ERRORS: {run.errors}"
        print(
            f"  - {run.mode}: run_id={run.run_id[:12] if run.run_id else 'N/A'} ... {status}"
        )

    if result.recommendation:
        print(f"\n[Smoke Test] Recommendation: {result.recommendation.outcome}")
        if result.recommendation.supported_claims:
            print(f"  Supported: {result.recommendation.supported_claims}")
        if result.recommendation.withdrawn_claims:
            print(f"  Withdrawn: {result.recommendation.withdrawn_claims}")
        if result.recommendation.known_limitations:
            print(f"  Limitations: {result.recommendation.known_limitations}")

    # Persist artifacts
    campaign_dir = Path("/tmp/firecrawl_smoke_test") / campaign_label
    campaign_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    artifacts_dir = campaign_dir / ts
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    result_path = artifacts_dir / "result.json"
    result_path.write_text(
        json.dumps(_result_to_serializable(result), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"\n[Smoke Test] Artifacts written to: {artifacts_dir}")

    return _result_to_serializable(result)


def _result_to_serializable(result) -> dict:
    """Convert a ReleaseBenchmarkResult to a JSON-serializable dict."""
    runs = []
    for run in result.runs:
        runs.append(
            {
                "mode": run.mode,
                "run_id": str(run.run_id),
                "objective_id": run.objective_id,
                "errors": run.errors,
                "quality": _quality_to_dict(run.quality) if run.quality else None,
                "performance": _perf_to_dict(run.performance)
                if run.performance
                else None,
            }
        )

    recommendation = None
    if result.recommendation:
        recommendation = {
            "outcome": result.recommendation.outcome,
            "supported_claims": result.recommendation.supported_claims,
            "withdrawn_claims": result.recommendation.withdrawn_claims,
            "known_limitations": result.recommendation.known_limitations,
            "conditions": result.recommendation.conditions,
            "p0_regressions": result.recommendation.p0_regressions,
        }

    return {
        "schema_version": result.schema_version,
        "campaign_id": result.campaign_id,
        "campaign_timestamp": result.campaign_timestamp,
        "environment": result.environment,
        "recommendation": recommendation,
        "total_duration_ms": result.total_duration_ms,
        "runs": runs,
    }


def _quality_to_dict(quality) -> dict:
    if quality is None:
        return None
    return {
        "schema_version": quality.schema_version,
        "candidate_recall": quality.candidate_recall,
        "source_quality_score": quality.source_quality_score,
        "coverage_completeness": quality.coverage_completeness,
        "unsupported_claim_rate": quality.unsupported_claim_rate,
        "citation_accuracy": quality.citation_accuracy,
        "report_quality_score": quality.report_quality_score,
    }


def _perf_to_dict(perf) -> dict:
    if perf is None:
        return None
    return {
        "schema_version": perf.schema_version,
        "total_latency_ms": perf.total_latency_ms,
        "total_tokens": perf.total_tokens,
        "semantic_calls": perf.semantic_calls,
        "cache_hit_rate": perf.cache_hit_rate,
        "cache_miss_rate": perf.cache_miss_rate,
        "embedding_throughput": perf.embedding_throughput,
        "gpu_memory_mb": perf.gpu_memory_mb,
        "cpu_percent": perf.cpu_percent,
    }


def compare_reproducibility(
    result_a: dict,
    result_b: dict,
    tolerance: float = 0.15,
) -> dict:
    """Compare two campaign results for reproducibility."""
    print(f"\n{'=' * 60}")
    print("[Reproducibility] Comparing two campaign runs")
    print(f"{'=' * 60}")

    # Check campaign IDs differ
    ids_match = result_a["campaign_id"] == result_b["campaign_id"]
    print(f"  Campaign IDs identical: {ids_match} (expected: False)")

    # Check modes match
    modes_a = {r["mode"] for r in result_a["runs"]}
    modes_b = {r["mode"] for r in result_b["runs"]}
    modes_match = modes_a == modes_b
    print(f"  Mode sets match: {modes_match} (A={modes_a}, B={modes_b})")

    # Check recommendations match
    rec_a = result_a.get("recommendation", {}).get("outcome", "UNKNOWN")
    rec_b = result_b.get("recommendation", {}).get("outcome", "UNKNOWN")
    rec_match = rec_a == rec_b
    print(f"  Recommendations match: {rec_match} (A={rec_a}, B={rec_b})")

    # Check quality metrics are within tolerance
    quality_issues = []
    for run_a, run_b in zip(
        sorted(result_a["runs"], key=lambda r: r["mode"]),
        sorted(result_b["runs"], key=lambda r: r["mode"]),
    ):
        qa = run_a.get("quality")
        qb = run_b.get("quality")
        if qa and qb:
            for key in [
                "candidate_recall",
                "source_quality_score",
                "coverage_completeness",
            ]:
                va = qa.get(key)
                vb = qb.get(key)
                if va and vb and va > 0:
                    rel_diff = abs(va - vb) / va
                    if rel_diff > tolerance:
                        quality_issues.append(
                            f"{run_a['mode']}.{key}: {va} vs {vb} (rel_diff={rel_diff:.3f} > {tolerance})"
                        )

    # Check performance metrics
    perf_issues = []
    for run_a, run_b in zip(
        sorted(result_a["runs"], key=lambda r: r["mode"]),
        sorted(result_b["runs"], key=lambda r: r["mode"]),
    ):
        pa = run_a.get("performance")
        pb = run_b.get("performance")
        if pa and pb:
            for key in ["total_tokens", "semantic_calls"]:
                va = pa.get(key)
                vb = pb.get(key)
                if va and vb and va > 0:
                    rel_diff = abs(va - vb) / va
                    if rel_diff > tolerance:
                        perf_issues.append(
                            f"{run_a['mode']}.{key}: {va} vs {vb} (rel_diff={rel_diff:.3f} > {tolerance})"
                        )

    reproducibility_pass = (
        not ids_match
        and modes_match
        and rec_match
        and not quality_issues
        and not perf_issues
    )

    print(f"\n  Quality issues: {len(quality_issues)}")
    for issue in quality_issues:
        print(f"    - {issue}")
    print(f"  Performance issues: {len(perf_issues)}")
    for issue in perf_issues:
        print(f"    - {issue}")
    print(f"\n  Reproducibility: {'PASS' if reproducibility_pass else 'FAIL'}")

    return {
        "reproducibility_pass": reproducibility_pass,
        "campaign_ids_match": ids_match,
        "modes_match": modes_match,
        "recommendations_match": rec_match,
        "quality_issues": quality_issues,
        "performance_issues": perf_issues,
    }


def main() -> int:
    """Run the smoke test."""
    candidate_sha = os.environ.get("CANDIDATE_SHA", "")
    if not candidate_sha or len(candidate_sha) != 40:
        # Try to get from git
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=SCRIPTS.parent,
            check=False,
        )
        candidate_sha = result.stdout.strip()
        if len(candidate_sha) != 40:
            print(
                f"[ERROR] Cannot determine candidate SHA (got {candidate_sha!r})",
                file=sys.stderr,
            )
            return 1

    database_url = os.environ.get("DATABASE_URL", "")
    blob_root = Path(os.environ.get("BLOB_ROOT", "/tmp/smoke-blobs"))
    qdrant_url = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
    qdrant_api_key = os.environ.get("QDRANT_API_KEY", "")

    if not database_url:
        print(
            "[ERROR] DATABASE_URL not set. Source scripts/research-env first.",
            file=sys.stderr,
        )
        return 1

    blob_root.mkdir(parents=True, exist_ok=True)

    # Export CANDIDATE_SHA so the benchmark runner can find it
    os.environ["CANDIDATE_SHA"] = candidate_sha

    print(f"\n[Smoke Test] Candidate SHA: {candidate_sha}")
    print(f"[Smoke Test] Database: {database_url[:50]}...")
    print(f"[Smoke Test] Blob root: {blob_root}")
    print(f"[Smoke Test] Qdrant: {qdrant_url}")
    print(f"[Smoke Test] Benchmark: {BENCHMARK_FIXTURE}")

    # Repetition 1
    result_a = run_single_campaign(
        campaign_label="smoke-repetition-1",
        candidate_sha=candidate_sha,
        database_url=database_url,
        blob_root=blob_root,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
    )

    # Repetition 2
    result_b = run_single_campaign(
        campaign_label="smoke-repetition-2",
        candidate_sha=candidate_sha,
        database_url=database_url,
        blob_root=blob_root,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
    )

    # Compare reproducibility
    repro = compare_reproducibility(result_a, result_b)

    # Final summary
    print(f"\n{'=' * 60}")
    print("[Smoke Test] Summary")
    print(f"{'=' * 60}")

    rec_a = result_a.get("recommendation", {}).get("outcome", "UNKNOWN")
    rec_b = result_b.get("recommendation", {}).get("outcome", "UNKNOWN")

    print(f"  Repetition 1 recommendation: {rec_a}")
    print(f"  Repetition 2 recommendation: {rec_b}")
    print(f"  Reproducibility: {'PASS' if repro['reproducibility_pass'] else 'FAIL'}")

    all_pass = rec_a == "GO" and rec_b == "GO" and repro["reproducibility_pass"]

    print(f"\n  Overall: {'PASS' if all_pass else 'FAIL'}")
    print(f"{'=' * 60}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
