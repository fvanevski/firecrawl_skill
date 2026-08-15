from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

COMMANDS = {"benchmark"}


def run(args, config, deps) -> int:
    if args.benchmark_subcommand == "run":
        from ..benchmark_admin import run_campaign

        output, outcome = run_campaign(config, args)
        if args.benchmark_output:
            output_path = Path(args.benchmark_output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(output_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as file:
                    json.dump(output, file, indent=2, default=str)
                os.replace(tmp_path, str(output_path))
                print(f"Results written to {output_path}")
            except BaseException:
                os.unlink(tmp_path)
                raise
        else:
            print(deps.dumps(output))
        return 0 if outcome in {"go", "go_with_conditions"} else 2

    if args.benchmark_subcommand == "results":
        if not args.benchmark_results_path:
            print(
                "ERROR: --results-path is required for 'results' subcommand",
                file=sys.stderr,
            )
            return 2
        results_path = Path(args.benchmark_results_path)
        if not results_path.exists():
            print(f"ERROR: results file not found: {results_path}", file=sys.stderr)
            return 2
        with open(results_path, "r", encoding="utf-8") as file:
            data = json.load(file)
        if args.benchmark_results_path:
            print(deps.dumps(data))
        return 0

    if args.benchmark_subcommand == "report":
        if not args.benchmark_report_path:
            print(
                "ERROR: --results-path is required for 'report' subcommand",
                file=sys.stderr,
            )
            return 2
        report_path = Path(args.benchmark_report_path)
        if not report_path.exists():
            print(f"ERROR: results file not found: {report_path}", file=sys.stderr)
            return 2
        with open(report_path, "r", encoding="utf-8") as file:
            data = json.load(file)
        lines: list[str] = []
        lines.append("=" * 60)
        lines.append("RELEASE BENCHMARK REPORT")
        lines.append("=" * 60)
        lines.append("")
        lines.append(f"Dataset version: {data.get('dataset_version', 'unknown')}")
        lines.append(f"Duration: {data.get('total_duration_ms', 0):.1f}ms")
        lines.append("")
        rec = data.get("recommendation", {})
        lines.append(
            f"Recommendation: {rec.get('outcome', 'unknown').replace('_', ' ').upper()}"
        )
        if rec.get("supported_claims"):
            lines.append("Supported claims:")
            for claim in rec["supported_claims"]:
                lines.append(f"  ✓ {claim}")
        if rec.get("withdrawn_claims"):
            lines.append("Withdrawn claims:")
            for claim in rec["withdrawn_claims"]:
                lines.append(f"  ✗ {claim}")
        if rec.get("known_limitations"):
            lines.append("Known limitations:")
            for limit in rec["known_limitations"]:
                lines.append(f"  • {limit}")
        if rec.get("p0_regressions"):
            lines.append("P0 regressions:")
            for regression in rec["p0_regressions"]:
                lines.append(f"  ! {regression}")
        lines.append("")
        comp = data.get("comparison", {})
        lines.append("Workflow comparison:")
        lines.append("-" * 40)
        for item in comp.get("results", []):
            mode = item.get("workflow_mode", "unknown")
            qual = item.get("quality", {})
            perf = item.get("performance", {})
            lines.append(f"  {mode}:")
            lines.append(f"    Recall: {qual.get('candidate_recall', 0):.3f}")
            lines.append(
                f"    Source quality: {qual.get('source_quality_score', 0):.3f}"
            )
            lines.append(f"    Coverage: {qual.get('coverage_completeness', 0):.3f}")
            lines.append(
                f"    Unsupported claims: {qual.get('unsupported_claim_rate', 0):.3f}"
            )
            lines.append(
                f"    Citation accuracy: {qual.get('citation_accuracy', 0):.3f}"
            )
            lines.append(f"    Latency: {perf.get('total_latency_ms', 0):.0f}ms")
            lines.append(f"    Tokens: {perf.get('total_tokens', 0)}")
            lines.append(f"    Semantic calls: {perf.get('semantic_calls', 0)}")
            lines.append("")
        lines.append(
            f"Integrity regression: {'YES' if comp.get('integrity_regression', False) else 'NO'}"
        )
        lines.append("")
        report_text = "\n".join(lines)
        print(report_text)
        if args.benchmark_report_output:
            output_path = Path(args.benchmark_report_output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(report_text, encoding="utf-8")
            print(f"Report written to {output_path}")
        return 0

    print(
        f"ERROR: unknown benchmark subcommand: {args.benchmark_subcommand}",
        file=sys.stderr,
    )
    return 2
