"""Application assembly for release benchmark campaigns."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .workflow_benchmark import (
    WorkflowBenchmarkConfig,
    WorkflowBenchmarkRunner,
    load_benchmark_dataset,
)


def run_campaign(config, args) -> tuple[dict[str, Any], str]:
    loader = load_benchmark_dataset(Path(args.benchmark_dataset))
    modes = tuple(args.benchmark_modes) if args.benchmark_modes else None
    benchmark_config = WorkflowBenchmarkConfig(
        workflow_modes=modes or loader.dataset.workflow_modes,
        dry_run=not args.benchmark_no_dry_run,
        blob_root=args.benchmark_blob_root or str(config.blob_root),
    )
    result = WorkflowBenchmarkRunner(loader, benchmark_config).run()
    output: dict[str, Any] = {
        "dataset_version": result.dataset_version,
        "total_duration_ms": result.total_duration_ms,
        "comparison": {
            "dataset_version": result.comparison.dataset_version,
            "integrity_regression": result.comparison.integrity_regression,
            "quality_vs_baseline": result.comparison.quality_vs_baseline,
            "performance_vs_baseline": result.comparison.performance_vs_baseline,
            "results": [
                {
                    "workflow_mode": item.workflow_mode,
                    "quality": {
                        "candidate_recall": item.quality.candidate_recall,
                        "source_quality_score": item.quality.source_quality_score,
                        "coverage_completeness": item.quality.coverage_completeness,
                        "unsupported_claim_rate": item.quality.unsupported_claim_rate,
                        "citation_accuracy": item.quality.citation_accuracy,
                        "report_quality_score": item.quality.report_quality_score,
                    },
                    "performance": {
                        "total_latency_ms": item.performance.total_latency_ms,
                        "total_tokens": item.performance.total_tokens,
                        "semantic_calls": item.performance.semantic_calls,
                        "cache_hit_rate": item.performance.cache_hit_rate,
                        "cache_miss_rate": item.performance.cache_miss_rate,
                        "embedding_throughput": item.performance.embedding_throughput,
                        "gpu_memory_mb": item.performance.gpu_memory_mb,
                        "cpu_percent": item.performance.cpu_percent,
                    },
                    "integrity_checks": [
                        {
                            "check_name": check.check_name,
                            "passed": check.passed,
                            "details": check.details,
                        }
                        for check in item.integrity_checks
                    ],
                }
                for item in result.comparison.results
            ],
        },
        "recommendation": {
            "outcome": result.recommendation.outcome,
            "dataset_version": result.recommendation.dataset_version,
            "supported_claims": list(result.recommendation.supported_claims),
            "withdrawn_claims": list(result.recommendation.withdrawn_claims),
            "known_limitations": list(result.recommendation.known_limitations),
            "conditions": list(result.recommendation.conditions),
            "p0_regressions": list(result.recommendation.p0_regressions),
        },
    }
    return output, result.recommendation.outcome
