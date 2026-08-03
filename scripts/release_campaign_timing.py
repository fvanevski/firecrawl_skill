"""Generate and validate PostgreSQL-derived release-campaign timing evidence.

The JSON artifact produced here is retained diagnostic evidence. PostgreSQL
remains authoritative for run and semantic-call state; this module never uses
its JSON output as runtime input, replay state, or an acquisition handoff.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

TIMING_DIAGNOSTICS_SCHEMA = "release-campaign-timing-v2"
TIMING_SOURCE_TABLES = ("research_runs", "semantic_calls")


class TimingDiagnosticsError(RuntimeError):
    """Timing evidence could not be produced without weakening the contract."""


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TimingDiagnosticsError(f"expected JSON object: {path}")
    return value


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _campaign_runs(campaign_dir: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    manifest = _load_object(campaign_dir / "manifest.json")
    descriptors: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for key, label in (("campaign_a", "A"), ("campaign_b", "B")):
        entry = manifest.get(key)
        if not isinstance(entry, Mapping):
            raise TimingDiagnosticsError(f"campaign manifest lacks {key}")
        result_dir = Path(str(entry.get("result_path") or ""))
        result_path = result_dir / "result.json"
        if not result_path.is_file():
            candidates = sorted((campaign_dir / label).glob("*/result.json"))
            if len(candidates) != 1:
                count = len(candidates)
                raise TimingDiagnosticsError(
                    f"expected exactly one campaign {label} result, found {count}"
                )
            result_path = candidates[0]
        result = _load_object(result_path)
        raw_runs = result.get("runs")
        if not isinstance(raw_runs, list):
            raise TimingDiagnosticsError(f"campaign {label} runs are missing")
        for raw in raw_runs:
            if not isinstance(raw, Mapping):
                raise TimingDiagnosticsError(
                    f"campaign {label} contains a non-object run"
                )
            run_id = str(raw.get("run_id") or "")
            UUID(run_id)
            mode = str(raw.get("mode") or "")
            objective_id = str(raw.get("objective_id") or "")
            key_tuple = (label, mode, objective_id)
            if not mode or not objective_id or key_tuple in seen:
                raise TimingDiagnosticsError(
                    f"duplicate or incomplete campaign run descriptor: {key_tuple!r}"
                )
            seen.add(key_tuple)
            descriptors.append(
                {
                    "campaign": label,
                    "mode": mode,
                    "objective_id": objective_id,
                    "run_id": run_id,
                }
            )
    return manifest, descriptors


def _duration_ms(started_at: Any, completed_at: Any) -> float | None:
    if not isinstance(started_at, datetime) or not isinstance(completed_at, datetime):
        return None
    elapsed = (completed_at - started_at).total_seconds() * 1000
    if elapsed < 0:
        return None
    return round(elapsed, 3)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        return None
    return converted


def _attempt_observations(response_metadata: Any) -> dict[str, Any]:
    attempts = (
        response_metadata.get("attempts")
        if isinstance(response_metadata, Mapping)
        else None
    )
    if not isinstance(attempts, Sequence) or isinstance(attempts, (str, bytes)):
        return {
            "has_attempt_metadata": False,
            "attempt_count": 0,
            "latency_observation_count": 0,
            "missing_latency_count": 0,
            "retry_count": 0,
            "latency_ms": None,
        }

    total = 0.0
    observed = 0
    missing = 0
    for item in attempts:
        latency = _number(item.get("latency_ms")) if isinstance(item, Mapping) else None
        if latency is None:
            missing += 1
        else:
            observed += 1
            total += latency
    return {
        "has_attempt_metadata": True,
        "attempt_count": len(attempts),
        "latency_observation_count": observed,
        "missing_latency_count": missing,
        "retry_count": max(len(attempts) - 1, 0),
        "latency_ms": round(total, 3) if observed else None,
    }


def _ratio(first: float | None, second: float | None) -> float | None:
    if first is None or second is None:
        return None
    smaller = min(abs(first), abs(second))
    larger = max(abs(first), abs(second))
    if larger <= 1e-9:
        return 1.0
    if smaller <= 1e-9:
        return None
    return round(larger / smaller, 4)


def _new_stage() -> dict[str, Any]:
    return {
        "call_count": 0,
        "call_ids": [],
        "idempotency_keys": [],
        "status_counts": Counter(),
        "attempt_count": 0,
        "attempt_latency_observation_count": 0,
        "missing_attempt_latency_count": 0,
        "calls_missing_attempt_metadata": 0,
        "retry_count": 0,
        "attempt_latency_ms": 0.0,
        "wall_clock_observation_count": 0,
        "missing_wall_clock_count": 0,
        "wall_clock_ms": 0.0,
    }


def _finalize_stage(raw: Mapping[str, Any]) -> dict[str, Any]:
    call_count = int(raw["call_count"])
    attempt_count = int(raw["attempt_count"])
    attempt_observations = int(raw["attempt_latency_observation_count"])
    missing_attempts = int(raw["missing_attempt_latency_count"])
    missing_attempt_metadata = int(raw["calls_missing_attempt_metadata"])
    wall_observations = int(raw["wall_clock_observation_count"])
    missing_wall = int(raw["missing_wall_clock_count"])
    complete = (
        call_count > 0
        and attempt_count > 0
        and missing_attempt_metadata == 0
        and missing_attempts == 0
        and attempt_observations == attempt_count
        and missing_wall == 0
        and wall_observations == call_count
    )
    return {
        "call_count": call_count,
        "call_ids": list(raw["call_ids"]),
        "idempotency_keys": list(raw["idempotency_keys"]),
        "status_counts": dict(sorted(raw["status_counts"].items())),
        "attempt_count": attempt_count,
        "attempt_latency_observation_count": attempt_observations,
        "missing_attempt_latency_count": missing_attempts,
        "calls_missing_attempt_metadata": missing_attempt_metadata,
        "retry_count": int(raw["retry_count"]),
        "attempt_latency_ms": (
            round(float(raw["attempt_latency_ms"]), 3) if attempt_observations else None
        ),
        "wall_clock_observation_count": wall_observations,
        "missing_wall_clock_count": missing_wall,
        "wall_clock_ms": (
            round(float(raw["wall_clock_ms"]), 3) if wall_observations else None
        ),
        "telemetry_complete": complete,
    }


def _tolerance_map(comparison: Mapping[str, Any]) -> dict[str, Sequence[Any]]:
    result: dict[str, Sequence[Any]] = {}
    values = [
        *(comparison.get("quality_tolerances") or []),
        *(comparison.get("performance_tolerances") or []),
    ]
    for item in values:
        if (
            not isinstance(item, Sequence)
            or isinstance(item, (str, bytes))
            or len(item) < 4
        ):
            continue
        metric = str(item[0])
        if metric in result:
            raise TimingDiagnosticsError(f"duplicate comparison tolerance: {metric}")
        result[metric] = item
    return result


def _comparison_failure_map(comparison: Mapping[str, Any]) -> dict[str, str]:
    raw_details = comparison.get("details") or []
    if not isinstance(raw_details, list):
        raise TimingDiagnosticsError("comparison details are malformed")
    result: dict[str, str] = {}
    for raw in raw_details:
        detail = str(raw)
        metric = detail.split(":", 1)[0]
        if len(metric.split(".", 2)) != 3:
            raise TimingDiagnosticsError(f"unparseable comparison failure: {detail}")
        if metric in result:
            raise TimingDiagnosticsError(f"duplicate comparison failure: {metric}")
        result[metric] = detail
    return result


def _stage_failure_comparison(
    run_a: Mapping[str, Any], run_b: Mapping[str, Any]
) -> tuple[str, str | None, list[dict[str, Any]]]:
    stages_a = run_a.get("semantic_stage_totals")
    stages_b = run_b.get("semantic_stage_totals")
    if not isinstance(stages_a, Mapping) or not isinstance(stages_b, Mapping):
        raise TimingDiagnosticsError("paired run lacks semantic stage totals")
    names = sorted(set(stages_a) | set(stages_b))
    if not names:
        return (
            "not_applicable",
            "no semantic_calls rows exist for either paired run",
            [],
        )

    comparisons: list[dict[str, Any]] = []
    for stage in names:
        value_a = stages_a.get(stage)
        value_b = stages_b.get(stage)
        present_a = isinstance(value_a, Mapping)
        present_b = isinstance(value_b, Mapping)
        attempt_a = value_a.get("attempt_latency_ms") if present_a else None
        attempt_b = value_b.get("attempt_latency_ms") if present_b else None
        wall_a = value_a.get("wall_clock_ms") if present_a else None
        wall_b = value_b.get("wall_clock_ms") if present_b else None
        comparisons.append(
            {
                "stage": stage,
                "campaign_a_present": present_a,
                "campaign_b_present": present_b,
                "campaign_a_attempt_latency_ms": attempt_a,
                "campaign_b_attempt_latency_ms": attempt_b,
                "attempt_latency_ratio": _ratio(attempt_a, attempt_b),
                "campaign_a_wall_clock_ms": wall_a,
                "campaign_b_wall_clock_ms": wall_b,
                "wall_clock_ratio": _ratio(wall_a, wall_b),
                "campaign_a_telemetry_complete": (
                    value_a.get("telemetry_complete") if present_a else None
                ),
                "campaign_b_telemetry_complete": (
                    value_b.get("telemetry_complete") if present_b else None
                ),
                "campaign_a_status_counts": (
                    value_a.get("status_counts") if present_a else None
                ),
                "campaign_b_status_counts": (
                    value_b.get("status_counts") if present_b else None
                ),
                "campaign_a_attempt_count": (
                    value_a.get("attempt_count") if present_a else None
                ),
                "campaign_b_attempt_count": (
                    value_b.get("attempt_count") if present_b else None
                ),
                "campaign_a_missing_attempt_latency_count": (
                    value_a.get("missing_attempt_latency_count") if present_a else None
                ),
                "campaign_b_missing_attempt_latency_count": (
                    value_b.get("missing_attempt_latency_count") if present_b else None
                ),
                "campaign_a_calls_missing_attempt_metadata": (
                    value_a.get("calls_missing_attempt_metadata") if present_a else None
                ),
                "campaign_b_calls_missing_attempt_metadata": (
                    value_b.get("calls_missing_attempt_metadata") if present_b else None
                ),
                "campaign_a_retry_count": (
                    value_a.get("retry_count") if present_a else None
                ),
                "campaign_b_retry_count": (
                    value_b.get("retry_count") if present_b else None
                ),
                "campaign_a_missing_wall_clock_count": (
                    value_a.get("missing_wall_clock_count") if present_a else None
                ),
                "campaign_b_missing_wall_clock_count": (
                    value_b.get("missing_wall_clock_count") if present_b else None
                ),
            }
        )
    return "available", None, comparisons


def write_timing_diagnostics(campaign_dir: Path, database_url: str) -> dict[str, Any]:
    """Write and self-validate durable PostgreSQL-derived timing evidence."""
    if not database_url:
        raise TimingDiagnosticsError("DATABASE_URL is required for timing diagnostics")

    import psycopg

    manifest, descriptors = _campaign_runs(campaign_dir)
    run_ids = [UUID(item["run_id"]) for item in descriptors]
    lifecycle: dict[str, dict[str, Any]] = {}
    raw_stages: dict[str, dict[str, dict[str, Any]]] = {
        str(run_id): {} for run_id in run_ids
    }

    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT id, state, started_at, completed_at
               FROM research_runs WHERE id = ANY(%s::uuid[])""",
            (run_ids,),
        )
        for run_id, state, started_at, completed_at in cursor.fetchall():
            lifecycle[str(run_id)] = {
                "state": str(state or "").lower(),
                "started_at": started_at.isoformat() if started_at else None,
                "completed_at": completed_at.isoformat() if completed_at else None,
                "duration_ms": _duration_ms(started_at, completed_at),
            }

        cursor.execute(
            """SELECT id, run_id, stage, status, idempotency_key,
                      started_at, completed_at, response_metadata
               FROM semantic_calls
               WHERE run_id = ANY(%s::uuid[])
               ORDER BY run_id, started_at NULLS LAST, created_at, id""",
            (run_ids,),
        )
        for (
            call_id,
            run_id,
            stage,
            status,
            idempotency_key,
            started_at,
            completed_at,
            metadata,
        ) in cursor.fetchall():
            stage_name = str(stage or "")
            if not stage_name:
                raise TimingDiagnosticsError(f"semantic call {call_id} has no stage")
            totals = raw_stages[str(run_id)].setdefault(stage_name, _new_stage())
            totals["call_count"] += 1
            totals["call_ids"].append(str(call_id))
            totals["idempotency_keys"].append(str(idempotency_key or ""))
            totals["status_counts"][str(status or "missing").lower()] += 1

            attempts = _attempt_observations(metadata)
            totals["attempt_count"] += attempts["attempt_count"]
            totals["attempt_latency_observation_count"] += attempts[
                "latency_observation_count"
            ]
            totals["missing_attempt_latency_count"] += attempts["missing_latency_count"]
            totals["retry_count"] += attempts["retry_count"]
            if not attempts["has_attempt_metadata"]:
                totals["calls_missing_attempt_metadata"] += 1
            if attempts["latency_ms"] is not None:
                totals["attempt_latency_ms"] += attempts["latency_ms"]

            wall_clock = _duration_ms(started_at, completed_at)
            if wall_clock is None:
                totals["missing_wall_clock_count"] += 1
            else:
                totals["wall_clock_observation_count"] += 1
                totals["wall_clock_ms"] += wall_clock

    missing = sorted(set(raw_stages) - set(lifecycle))
    if missing:
        raise TimingDiagnosticsError(
            f"timing diagnostics lack research_runs rows: {missing}"
        )

    stages = {
        run_id: {
            name: _finalize_stage(value) for name, value in sorted(stage_values.items())
        }
        for run_id, stage_values in raw_stages.items()
    }
    runs = [
        {
            **descriptor,
            **lifecycle[descriptor["run_id"]],
            "semantic_stage_totals": stages[descriptor["run_id"]],
        }
        for descriptor in descriptors
    ]
    by_key = {(run["campaign"], run["mode"], run["objective_id"]): run for run in runs}

    comparison_paths = sorted(
        (campaign_dir / "reproducibility").glob("*/comparison.json")
    )
    if len(comparison_paths) != 1:
        raise TimingDiagnosticsError(
            f"expected exactly one comparison.json, found {len(comparison_paths)}"
        )
    comparison = _load_object(comparison_paths[0])
    tolerances = _tolerance_map(comparison)
    failures: list[dict[str, Any]] = []
    for metric, detail in _comparison_failure_map(comparison).items():
        mode, objective_id, _metric_name = metric.split(".", 2)
        run_a = by_key.get(("A", mode, objective_id))
        run_b = by_key.get(("B", mode, objective_id))
        if run_a is None or run_b is None:
            raise TimingDiagnosticsError(
                f"comparison failure {metric} lacks a paired campaign run"
            )
        tolerance = tolerances.get(metric)
        if tolerance is None:
            raise TimingDiagnosticsError(
                f"comparison failure {metric} lacks a tolerance record"
            )
        value_a = _number(tolerance[1])
        value_b = _number(tolerance[2])
        if value_a is None or value_b is None:
            raise TimingDiagnosticsError(
                f"comparison failure {metric} has invalid compared values"
            )
        stage_status, stage_reason, stage_comparison = _stage_failure_comparison(
            run_a, run_b
        )
        failures.append(
            {
                "metric": metric,
                "detail": detail,
                "campaign_a_run_id": run_a["run_id"],
                "campaign_b_run_id": run_b["run_id"],
                "campaign_a_value": value_a,
                "campaign_b_value": value_b,
                "value_ratio": _ratio(value_a, value_b),
                "semantic_stage_diagnostics_status": stage_status,
                "semantic_stage_diagnostics_reason": stage_reason,
                "semantic_stage_latency_comparison": stage_comparison,
            }
        )

    diagnostics = {
        "schema_version": TIMING_DIAGNOSTICS_SCHEMA,
        "candidate_sha": manifest.get("candidate_sha"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_tables": list(TIMING_SOURCE_TABLES),
        "run_count": len(runs),
        "runs": runs,
        "reproducibility_failures": failures,
    }
    output_path = campaign_dir / "timing-diagnostics.json"
    _write_json_atomic(output_path, diagnostics)
    from release_campaign_timing_contract import validate_timing_diagnostics

    validation_errors = validate_timing_diagnostics(
        diagnostics,
        candidate_sha=str(manifest.get("candidate_sha") or ""),
        run_ids=[item["run_id"] for item in descriptors],
        comparison=comparison,
    )
    if validation_errors:
        raise TimingDiagnosticsError(
            "timing diagnostics validation failed: " + "; ".join(validation_errors)
        )
    return diagnostics
