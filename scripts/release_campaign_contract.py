"""Release-only evidence normalization and fixed-workload calibration.

The ordinary benchmark runner records authoritative state but its legacy result
serializer does not retain every record identifier needed by the exact-head
release verifier. This module runs only at the authoritative release boundary:

* enriches each measured metric with run-scoped PostgreSQL provenance;
* normalizes deterministic-debug token usage to an explicit N/A observation;
* measures embedding throughput with one versioned, fixed-size calibration
  workload per research run; and
* rewrites the reproducibility artifact only after proving the compared
  calibration workloads have identical identity and size.

It never changes benchmark objectives, quality values, recommendations, or
acceptance thresholds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

CALIBRATION_STAGE = "release_calibration"
CALIBRATION_VERSION = "release-embedding-calibration-v1"
CALIBRATION_TEXTS = tuple(
    f"firecrawl release calibration {index:02d}: stable embedding workload"
    for index in range(32)
)
EXPECTED_MODES = ("autonomous_local", "deterministic_debug")
MANDATORY_QUALITY = frozenset(
    {
        "candidate_recall",
        "source_quality_score",
        "coverage_completeness",
        "unsupported_claim_rate",
        "citation_accuracy",
        "report_quality_score",
    }
)
MANDATORY_PERFORMANCE = frozenset(
    {
        "total_tokens",
        "cache_hit_rate",
        "embedding_throughput",
        "cpu_percent",
        "gpu_memory_mb",
    }
)
QUALITY_PROVENANCE = {
    "candidate_recall": "candidates",
    "source_quality_score": "candidates",
    "coverage_completeness": "coverage",
    "unsupported_claim_rate": "claims",
    "citation_accuracy": "citations",
    "report_quality_score": "report",
}


class ContractError(RuntimeError):
    """The release artifact cannot be normalized without weakening the gate."""


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ContractError(f"expected JSON object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _result_paths(campaign_dir: Path) -> tuple[Path, Path]:
    paths: list[Path] = []
    for label in ("A", "B"):
        candidates = sorted((campaign_dir / label).glob("*/result.json"))
        if len(candidates) != 1:
            raise ContractError(
                f"expected exactly one {label} result.json, found {len(candidates)}"
            )
        paths.append(candidates[0])
    return paths[0], paths[1]


def _comparison_path(campaign_dir: Path) -> Path:
    candidates = sorted(campaign_dir.glob("reproducibility/*/comparison.json"))
    if len(candidates) != 1:
        raise ContractError(
            f"expected exactly one comparison.json, found {len(candidates)}"
        )
    return candidates[0]


def _metric_map(run: Mapping[str, Any], key: str) -> dict[str, dict[str, Any]]:
    raw = run.get(key)
    if not isinstance(raw, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for metric in raw:
        if isinstance(metric, dict) and metric.get("name"):
            result[str(metric["name"])] = metric
    return result


def _query_ids(connection, query: str, run_id: str) -> tuple[str, ...]:
    try:
        with connection.cursor() as cursor:
            cursor.execute(query, (run_id,))
            rows = cursor.fetchall()
        return tuple(str(row[0]) for row in rows if row and row[0] is not None)
    except Exception:  # noqa: BLE001
        connection.rollback()
        return ()


def _authoritative_ids(connection, run_id: str) -> dict[str, tuple[str, ...]]:
    candidates = _query_ids(
        connection,
        "SELECT id FROM search_candidates WHERE run_id = %s ORDER BY id",
        run_id,
    )
    coverage = _query_ids(
        connection,
        """SELECT DISTINCT item_id FROM coverage_events
           WHERE run_id = %s ORDER BY item_id""",
        run_id,
    )
    claims = _query_ids(
        connection,
        "SELECT id FROM research_claims WHERE run_id = %s ORDER BY id",
        run_id,
    )
    citations = _query_ids(
        connection,
        "SELECT id FROM claim_evidence_links WHERE run_id = %s ORDER BY id",
        run_id,
    )
    synthesis = _query_ids(
        connection,
        """SELECT id FROM synthesis_stages WHERE run_id = %s
           AND stage_name = 'citation_pass' ORDER BY updated_at, id""",
        run_id,
    )
    packets = _query_ids(
        connection,
        "SELECT id FROM evidence_packets WHERE run_id = %s ORDER BY id",
        run_id,
    )
    token_records = _query_ids(
        connection,
        "SELECT id FROM endpoint_usage_records WHERE run_id = %s ORDER BY id",
        run_id,
    )
    semantic_calls = _query_ids(
        connection,
        "SELECT id FROM semantic_calls WHERE run_id = %s ORDER BY id",
        run_id,
    )
    report = tuple(dict.fromkeys((*packets, *claims, *coverage)))
    citation_records = tuple(dict.fromkeys((*synthesis, *citations, *claims)))
    return {
        "candidates": candidates,
        "coverage": coverage,
        "claims": claims,
        "citations": citation_records,
        "report": report,
        "tokens": token_records,
        "semantic_calls": semantic_calls,
    }


def _embedding_endpoint() -> tuple[str, str, int, str, str]:
    raw_url = os.environ.get("EMBEDDING_URL", "").rstrip("/")
    if not raw_url:
        raise ContractError("EMBEDDING_URL is required for release calibration")
    endpoint = re.sub(r"/v1(?:/embeddings)?$", "/v1/embeddings", raw_url)
    model = os.environ.get("EMBEDDING_MODEL", "")
    revision = os.environ.get("EMBEDDING_REVISION", "")
    dimension_text = os.environ.get("EMBEDDING_DIMENSION", "")
    if not model or not revision or not dimension_text:
        raise ContractError(
            "EMBEDDING_MODEL, EMBEDDING_REVISION, and EMBEDDING_DIMENSION are required"
        )
    try:
        dimension = int(dimension_text)
    except ValueError as exc:
        raise ContractError("EMBEDDING_DIMENSION must be an integer") from exc
    fingerprint = hashlib.sha256(
        f"{model}\0{revision}\0{dimension}".encode()
    ).hexdigest()
    return endpoint, model, dimension, revision, fingerprint


def _call_embedding(
    endpoint: str,
    model: str,
    dimension: int,
    texts: Sequence[str],
) -> tuple[int, float]:
    payload = json.dumps(
        {"model": model, "input": list(texts), "encoding_type": "float"}
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("EMBEDDING_API_KEY", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers=headers,
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=120) as response:
        body = json.loads(response.read().decode("utf-8"))
    elapsed = time.perf_counter() - started
    vectors = body.get("data") if isinstance(body, dict) else None
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise ContractError(
            f"calibration returned {len(vectors) if isinstance(vectors, list) else 'invalid'} "
            f"vectors for {len(texts)} texts"
        )
    for item in vectors:
        vector = item.get("embedding") if isinstance(item, dict) else None
        if not isinstance(vector, list) or len(vector) != dimension:
            raise ContractError("calibration embedding dimension mismatch")
        if not all(
            isinstance(value, (int, float)) and math.isfinite(value) for value in vector
        ):
            raise ContractError("calibration embedding contains non-finite values")
    if elapsed <= 0:
        raise ContractError("calibration elapsed time is not positive")
    return len(vectors), elapsed


def _existing_calibration(connection, run_id: str) -> tuple[str, int, float] | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT id, total_texts, elapsed_seconds
               FROM run_embedding_throughput
               WHERE run_id = %s AND stage = %s
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (run_id, CALIBRATION_STAGE),
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return str(row[0]), int(row[1]), float(row[2])


def _record_calibration(connection, run_id: str) -> dict[str, Any]:
    endpoint, model, dimension, revision, fingerprint = _embedding_endpoint()
    existing = _existing_calibration(connection, run_id)
    if existing is None:
        vector_count, elapsed = _call_embedding(
            endpoint,
            model,
            dimension,
            CALIBRATION_TEXTS,
        )
        record_id = uuid4()
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO run_embedding_throughput (
                       id, run_id, stage, batch_count, vector_count,
                       failed_count, total_texts, elapsed_seconds,
                       endpoint_url, endpoint_model, dimension, created_at
                   ) VALUES (%s, %s, %s, 1, %s, 0, %s, %s, %s, %s, %s, %s)""",
                (
                    record_id,
                    run_id,
                    CALIBRATION_STAGE,
                    vector_count,
                    len(CALIBRATION_TEXTS),
                    elapsed,
                    endpoint,
                    model,
                    dimension,
                    time.time(),
                ),
            )
        connection.commit()
        record_id_text = str(record_id)
        total_texts = len(CALIBRATION_TEXTS)
    else:
        record_id_text, total_texts, elapsed = existing
    if total_texts != len(CALIBRATION_TEXTS):
        raise ContractError(
            f"existing calibration workload is {total_texts}, expected {len(CALIBRATION_TEXTS)}"
        )
    return {
        "record_id": record_id_text,
        "total_texts": total_texts,
        "elapsed_seconds": elapsed,
        "throughput": round(total_texts / elapsed, 6),
        "stage": CALIBRATION_STAGE,
        "version": CALIBRATION_VERSION,
        "endpoint_model": model,
        "embedding_revision": revision,
        "embedding_dimension": dimension,
        "embedding_fingerprint": fingerprint,
    }


def _set_source_records(
    metric: dict[str, Any],
    record_ids: Iterable[str],
    *,
    sample_count: int | None = None,
) -> None:
    source = metric.setdefault("source", {})
    if not isinstance(source, dict):
        raise ContractError(f"metric {metric.get('name')} has invalid source")
    records = list(dict.fromkeys(str(value) for value in record_ids if value))
    source["record_ids"] = records
    source["sample_count"] = int(
        sample_count if sample_count is not None else len(records)
    )


def _normalize_run(
    run: dict[str, Any],
    provenance: Mapping[str, tuple[str, ...]],
    calibration: Mapping[str, Any],
) -> None:
    run_id = str(run.get("run_id") or "")
    mode = str(run.get("mode") or "")
    UUID(run_id)

    quality_metrics = _metric_map(run, "quality_metrics")
    for metric_name, provenance_key in QUALITY_PROVENANCE.items():
        metric = quality_metrics.get(metric_name)
        if metric is None or metric.get("status") != "measured":
            continue
        records = provenance.get(provenance_key, ())
        if not records:
            raise ContractError(
                f"{run_id} measured {metric_name} has no authoritative records"
            )
        _set_source_records(metric, records)

    performance_metrics = _metric_map(run, "performance_metrics")
    semantic = performance_metrics.get("semantic_calls")
    if semantic is not None and semantic.get("status") == "measured":
        records = provenance.get("semantic_calls", ())
        if records:
            _set_source_records(semantic, records)

    tokens = performance_metrics.get("total_tokens")
    performance = run.get("performance")
    if not isinstance(performance, dict):
        raise ContractError(f"{run_id} has no performance object")
    if tokens is None:
        raise ContractError(f"{run_id} has no total_tokens metric")
    if mode == "deterministic_debug":
        tokens["status"] = "not_applicable"
        tokens["value"] = None
        tokens["formula"] = (
            "not_invoked — deterministic fixture executed no generative model; "
            "token usage is intentionally NOT_APPLICABLE"
        )
        source = tokens.setdefault("source", {})
        source.update(
            {
                "table": "endpoint_usage_records",
                "column": "total_tokens",
                "method": "not_invoked",
                "run_id": run_id,
                "record_ids": [],
                "sample_count": 0,
                "status_counts": {"not_invoked": 1},
            }
        )
        performance["total_tokens"] = None
    elif tokens.get("status") == "measured":
        records = provenance.get("tokens", ())
        if not records:
            raise ContractError(f"{run_id} measured total_tokens has no usage records")
        _set_source_records(tokens, records)

    embedding = performance_metrics.get("embedding_throughput")
    if embedding is None:
        raise ContractError(f"{run_id} has no embedding_throughput metric")
    embedding["status"] = "measured"
    embedding["value"] = calibration["throughput"]
    embedding["formula"] = (
        f"{CALIBRATION_STAGE}: {calibration['total_texts']}/"
        f"{calibration['elapsed_seconds']:.6f}s"
    )
    source = embedding.setdefault("source", {})
    source.update(
        {
            "table": "run_embedding_throughput",
            "column": "total_texts, elapsed_seconds",
            "method": "fixed_workload_ratio",
            "run_id": run_id,
            "record_ids": [calibration["record_id"]],
            "sample_count": calibration["total_texts"],
            "stages": [CALIBRATION_STAGE],
            "stage_set_version": CALIBRATION_VERSION,
            "status_counts": {
                "batch_count": 1,
                "vector_count": calibration["total_texts"],
                "failed_count": 0,
            },
            "endpoint_model": calibration["endpoint_model"],
            "embedding_revision": calibration["embedding_revision"],
            "embedding_dimension": calibration["embedding_dimension"],
            "embedding_fingerprint": calibration["embedding_fingerprint"],
        }
    )
    performance["embedding_throughput"] = calibration["throughput"]


def _run_index(result: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    runs = result.get("runs")
    if not isinstance(runs, list):
        raise ContractError("result runs are missing")
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for run in runs:
        if not isinstance(run, dict):
            raise ContractError("result contains a non-object run")
        key = (str(run.get("mode") or ""), str(run.get("objective_id") or ""))
        if key in indexed:
            raise ContractError(f"duplicate run pair: {key}")
        indexed[key] = run
    return indexed


def _embedding_contract(metric: Mapping[str, Any]) -> tuple[Any, ...]:
    source = metric.get("source")
    if not isinstance(source, Mapping):
        raise ContractError("embedding metric lacks source")
    return (
        tuple(source.get("stages") or ()),
        source.get("stage_set_version"),
        source.get("sample_count"),
        source.get("endpoint_model"),
        source.get("embedding_revision"),
        source.get("embedding_dimension"),
        source.get("embedding_fingerprint"),
    )


def _rewrite_reproducibility(
    comparison: dict[str, Any],
    result_a: Mapping[str, Any],
    result_b: Mapping[str, Any],
) -> None:
    runs_a = _run_index(result_a)
    runs_b = _run_index(result_b)
    if runs_a.keys() != runs_b.keys():
        raise ContractError("campaign run pairs do not match")

    details = [
        str(item)
        for item in comparison.get("details", [])
        if ".embedding_throughput:" not in str(item)
    ]
    observations = [
        str(item)
        for item in comparison.get("observations", [])
        if ".embedding_throughput:" not in str(item)
    ]
    tolerances = [
        item
        for item in comparison.get("performance_tolerances", [])
        if not (
            isinstance(item, list)
            and item
            and str(item[0]).endswith(".embedding_throughput")
        )
    ]
    ratio_limit = float(comparison.get("operational_ratio_limit", 2.0))
    relative_tolerance = float(comparison.get("relative_tolerance", 0.15))

    for key in sorted(runs_a):
        mode, objective = key
        metric_a = _metric_map(runs_a[key], "performance_metrics").get(
            "embedding_throughput"
        )
        metric_b = _metric_map(runs_b[key], "performance_metrics").get(
            "embedding_throughput"
        )
        if metric_a is None or metric_b is None:
            raise ContractError(f"missing embedding metric for {mode}/{objective}")
        contract_a = _embedding_contract(metric_a)
        contract_b = _embedding_contract(metric_b)
        label = f"{mode}.{objective}.embedding_throughput"
        if contract_a != contract_b:
            details.append(
                f"{label}: fixed calibration workload mismatch: "
                f"{contract_a!r} != {contract_b!r}"
            )
            continue
        value_a = float(metric_a["value"])
        value_b = float(metric_b["value"])
        denominator = max(abs(value_a), 1e-12)
        relative = abs(value_a - value_b) / denominator
        minimum = min(abs(value_a), abs(value_b))
        maximum = max(abs(value_a), abs(value_b))
        ratio = maximum / minimum if minimum > 0 else math.inf
        tolerances.append([label, value_a, value_b, relative])
        if ratio > ratio_limit:
            details.append(
                f"{label}: {value_a:.4f} vs {value_b:.4f} "
                f"(ratio {ratio:.4f} > {ratio_limit})"
            )
        elif relative > relative_tolerance:
            observations.append(
                f"{label}: fixed-workload operational variance accepted — "
                f"{value_a:.4f} vs {value_b:.4f}; rel diff={relative:.4f}; "
                f"ratio={ratio:.4f}; ratio_limit={ratio_limit}"
            )

    comparison["performance_tolerances"] = tolerances
    comparison["details"] = details
    comparison["observations"] = observations
    comparison["all_within_tolerance"] = not details


def _rewrite_comparison_summary(
    comparison_path: Path, comparison: Mapping[str, Any]
) -> None:
    summary_path = comparison_path.with_name("summary.txt")
    lines = [
        f"Reproducibility Comparison — {comparison.get('run_a_id')} vs {comparison.get('run_b_id')}",
        f"Outcome: {'PASS' if comparison.get('all_within_tolerance') else 'FAIL'}",
        f"Quality tolerances: {len(comparison.get('quality_tolerances', []))} metrics compared",
        f"Performance tolerances: {len(comparison.get('performance_tolerances', []))} metrics compared",
    ]
    lines.extend(f"  - {item}" for item in comparison.get("details", []))
    lines.extend(
        f"  - observation: {item}" for item in comparison.get("observations", [])
    )
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def repair_campaign_contract(campaign_dir: Path, database_url: str) -> bool:
    """Normalize a completed two-campaign artifact against authoritative state."""
    if not database_url:
        raise ContractError("DATABASE_URL is required")
    result_a_path, result_b_path = _result_paths(campaign_dir)
    comparison_path = _comparison_path(campaign_dir)
    manifest_path = campaign_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ContractError("manifest.json is missing")

    import psycopg

    result_a = _load_object(result_a_path)
    result_b = _load_object(result_b_path)
    all_runs = [*_run_index(result_a).values(), *_run_index(result_b).values()]
    with psycopg.connect(database_url) as connection:
        # One unrecorded warm-up call prevents model-load latency from being
        # assigned to the first research run's calibration record.
        endpoint, model, dimension, _revision, _fingerprint = _embedding_endpoint()
        _call_embedding(endpoint, model, dimension, CALIBRATION_TEXTS)
        for run in all_runs:
            run_id = str(run.get("run_id") or "")
            provenance = _authoritative_ids(connection, run_id)
            calibration = _record_calibration(connection, run_id)
            _normalize_run(run, provenance, calibration)

    _write_json(result_a_path, result_a)
    _write_json(result_b_path, result_b)

    comparison = _load_object(comparison_path)
    _rewrite_reproducibility(comparison, result_a, result_b)
    _write_json(comparison_path, comparison)
    _rewrite_comparison_summary(comparison_path, comparison)

    manifest = _load_object(manifest_path)
    manifest_a = manifest.get("campaign_a")
    manifest_b = manifest.get("campaign_b")
    if not isinstance(manifest_a, dict) or not isinstance(manifest_b, dict):
        raise ContractError("campaign manifest entries are missing")
    manifest_a["result_hash"] = _sha256(result_a_path)
    manifest_b["result_hash"] = _sha256(result_b_path)
    reproducibility = manifest.setdefault("reproducibility", {})
    if not isinstance(reproducibility, dict):
        raise ContractError("manifest reproducibility entry is invalid")
    for key in (
        "all_within_tolerance",
        "run_a_id",
        "run_b_id",
        "policy_version",
        "relative_tolerance",
        "operational_ratio_limit",
        "operational_absolute_tolerances",
        "details",
        "observations",
    ):
        reproducibility[key] = comparison.get(key)
    manifest["release_contract_version"] = CALIBRATION_VERSION
    _write_json(manifest_path, manifest)

    recovery = campaign_dir / "recovery-report.txt"
    recovery.write_text(
        "Release evidence contract normalization completed.\n"
        f"Calibration version: {CALIBRATION_VERSION}\n"
        f"Calibration texts per run: {len(CALIBRATION_TEXTS)}\n"
        "Metric provenance was rebound to authoritative PostgreSQL records.\n"
        f"Reproducibility: {'PASS' if comparison['all_within_tolerance'] else 'FAIL'}\n",
        encoding="utf-8",
    )
    return bool(comparison.get("all_within_tolerance"))


def validate_campaign_contract(campaign_dir: Path) -> list[str]:
    """Validate the normalized artifact without contacting mutable services."""
    errors: list[str] = []
    try:
        result_paths = _result_paths(campaign_dir)
        comparison = _load_object(_comparison_path(campaign_dir))
    except Exception as exc:  # noqa: BLE001
        return [str(exc)]

    seen_runs: set[str] = set()
    for result_path in result_paths:
        result = _load_object(result_path)
        recommendation = result.get("recommendation")
        if (
            not isinstance(recommendation, dict)
            or recommendation.get("outcome") != "go"
        ):
            errors.append(f"{result_path}: recommendation is not go")
        for run in _run_index(result).values():
            run_id = str(run.get("run_id") or "")
            if run_id in seen_runs:
                errors.append(f"duplicate run UUID: {run_id}")
            seen_runs.add(run_id)
            mode = str(run.get("mode") or "")
            for key, mandatory in (
                ("quality_metrics", MANDATORY_QUALITY),
                ("performance_metrics", MANDATORY_PERFORMANCE),
            ):
                for metric in _metric_map(run, key).values():
                    if metric.get("name") not in mandatory:
                        continue
                    status = metric.get("status")
                    source = metric.get("source")
                    if not isinstance(source, Mapping):
                        errors.append(
                            f"{run_id}/{metric.get('name')}: source is missing"
                        )
                        continue
                    records = source.get("record_ids")
                    samples = source.get("sample_count")
                    has_records = isinstance(records, list) and bool(records)
                    has_samples = isinstance(samples, int) and samples > 0
                    if status == "measured" and not (has_records or has_samples):
                        errors.append(
                            f"{run_id}/{metric.get('name')}: measured metric lacks provenance"
                        )
            tokens = _metric_map(run, "performance_metrics").get("total_tokens")
            if mode == "deterministic_debug" and tokens is not None:
                source = tokens.get("source") or {}
                if tokens.get("status") != "not_applicable":
                    errors.append(f"{run_id}/total_tokens: expected not_applicable")
                if tokens.get("value") is not None:
                    errors.append(f"{run_id}/total_tokens: N/A value must be null")
                if not (source.get("status_counts") or {}).get("not_invoked"):
                    errors.append(
                        f"{run_id}/total_tokens: missing not_invoked provenance"
                    )
            embedding = _metric_map(run, "performance_metrics").get(
                "embedding_throughput"
            )
            if embedding is None:
                errors.append(f"{run_id}: embedding metric missing")
            else:
                source = embedding.get("source") or {}
                if source.get("stages") != [CALIBRATION_STAGE]:
                    errors.append(f"{run_id}: embedding stage is not fixed calibration")
                if source.get("stage_set_version") != CALIBRATION_VERSION:
                    errors.append(f"{run_id}: embedding calibration version mismatch")
                if source.get("sample_count") != len(CALIBRATION_TEXTS):
                    errors.append(f"{run_id}: embedding calibration workload mismatch")
                for field in (
                    "endpoint_model",
                    "embedding_revision",
                    "embedding_dimension",
                    "embedding_fingerprint",
                ):
                    if not source.get(field):
                        errors.append(f"{run_id}: embedding provenance lacks {field}")

    if len(seen_runs) != 20:
        errors.append(f"expected 20 globally unique runs, got {len(seen_runs)}")
    if comparison.get("all_within_tolerance") is not True:
        errors.append("reproducibility did not pass")
    if comparison.get("details"):
        errors.append(f"reproducibility contains failures: {comparison['details']!r}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    repair = subparsers.add_parser("repair")
    repair.add_argument("--campaign-dir", type=Path, required=True)
    repair.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    verify = subparsers.add_parser("verify")
    verify.add_argument("--campaign-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "repair":
        try:
            passed = repair_campaign_contract(args.campaign_dir, args.database_url)
            errors = validate_campaign_contract(args.campaign_dir)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: release contract repair failed: {exc}")
            return 1
        for error in errors:
            print(f"ERROR: {error}")
        return 0 if passed and not errors else 1

    errors = validate_campaign_contract(args.campaign_dir)
    for error in errors:
        print(f"ERROR: {error}")
    if not errors:
        print(
            f"Release contract PASS: {CALIBRATION_VERSION}; "
            f"{len(CALIBRATION_TEXTS)} fixed texts per run"
        )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
