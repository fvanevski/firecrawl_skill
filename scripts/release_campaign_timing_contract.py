"""Strict validation for versioned release-campaign timing evidence."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from release_campaign_timing import (
    TIMING_DIAGNOSTICS_SCHEMA,
    TIMING_SOURCE_TABLES,
    TimingDiagnosticsError,
    _comparison_failure_map,
    _number,
    _ratio,
    _tolerance_map,
)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _valid_timestamp(value: Any) -> bool:
    return _parse_timestamp(value) is not None


def _same_number(first: Any, second: Any) -> bool:
    one = _number(first)
    two = _number(second)
    return one is not None and two is not None and math.isclose(one, two, abs_tol=1e-6)


def _validate_stage(stage: Mapping[str, Any], prefix: str) -> list[str]:
    errors: list[str] = []
    call_count = stage.get("call_count")
    if (
        not isinstance(call_count, int)
        or isinstance(call_count, bool)
        or call_count <= 0
    ):
        return [f"{prefix} call_count is invalid"]

    call_ids = stage.get("call_ids")
    if (
        not isinstance(call_ids, list)
        or len(call_ids) != call_count
        or len(set(call_ids)) != call_count
    ):
        errors.append(f"{prefix} call IDs are incomplete or duplicated")
    else:
        for call_id in call_ids:
            try:
                UUID(str(call_id))
            except (ValueError, TypeError, AttributeError):
                errors.append(f"{prefix} contains invalid semantic-call ID {call_id!r}")

    keys = stage.get("idempotency_keys")
    if (
        not isinstance(keys, list)
        or len(keys) != call_count
        or any(not isinstance(value, str) or not value for value in keys)
        or len(set(keys)) != call_count
    ):
        errors.append(f"{prefix} idempotency keys are incomplete or duplicated")

    status_counts = stage.get("status_counts")
    if (
        not isinstance(status_counts, Mapping)
        or any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            for key, value in status_counts.items()
        )
        or sum(status_counts.values()) != call_count
    ):
        errors.append(f"{prefix} status counts do not cover all calls")

    integer_fields = (
        "attempt_count",
        "attempt_latency_observation_count",
        "missing_attempt_latency_count",
        "calls_missing_attempt_metadata",
        "retry_count",
        "wall_clock_observation_count",
        "missing_wall_clock_count",
    )
    integers: dict[str, int] = {}
    for field in integer_fields:
        value = stage.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"{prefix} {field} is invalid")
        else:
            integers[field] = value

    if len(integers) == len(integer_fields):
        if (
            integers["attempt_latency_observation_count"]
            + integers["missing_attempt_latency_count"]
            != integers["attempt_count"]
        ):
            errors.append(f"{prefix} attempt latency accounting is inconsistent")
        if (
            integers["wall_clock_observation_count"]
            + integers["missing_wall_clock_count"]
            != call_count
        ):
            errors.append(f"{prefix} wall-clock accounting is inconsistent")
        if integers["calls_missing_attempt_metadata"] > call_count:
            errors.append(f"{prefix} missing attempt-metadata count exceeds calls")
        if integers["retry_count"] > integers["attempt_count"]:
            errors.append(f"{prefix} retry count exceeds attempts")

        attempt_latency = stage.get("attempt_latency_ms")
        if integers["attempt_latency_observation_count"]:
            if _number(attempt_latency) is None:
                errors.append(f"{prefix} lacks observed attempt latency")
        elif attempt_latency is not None:
            errors.append(f"{prefix} has attempt latency without observations")

        wall_clock = stage.get("wall_clock_ms")
        if integers["wall_clock_observation_count"]:
            if _number(wall_clock) is None:
                errors.append(f"{prefix} lacks observed wall-clock latency")
        elif wall_clock is not None:
            errors.append(f"{prefix} has wall-clock latency without observations")

        expected_complete = (
            call_count > 0
            and integers["attempt_count"] > 0
            and integers["calls_missing_attempt_metadata"] == 0
            and integers["missing_attempt_latency_count"] == 0
            and integers["attempt_latency_observation_count"]
            == integers["attempt_count"]
            and integers["missing_wall_clock_count"] == 0
            and integers["wall_clock_observation_count"] == call_count
        )
        if stage.get("telemetry_complete") is not expected_complete:
            errors.append(f"{prefix} telemetry_complete is inconsistent")
    elif not isinstance(stage.get("telemetry_complete"), bool):
        errors.append(f"{prefix} telemetry_complete is invalid")
    return errors


def validate_timing_diagnostics(
    diagnostics: Mapping[str, Any],
    *,
    candidate_sha: str,
    run_ids: Sequence[str],
    comparison: Mapping[str, Any] | None,
) -> list[str]:
    """Cross-validate timing evidence against authoritative run IDs and comparison."""
    errors: list[str] = []
    if diagnostics.get("schema_version") != TIMING_DIAGNOSTICS_SCHEMA:
        errors.append("timing diagnostics schema version is invalid")
    if diagnostics.get("candidate_sha") != candidate_sha:
        errors.append("timing diagnostics candidate mismatch")
    if diagnostics.get("source_tables") != list(TIMING_SOURCE_TABLES):
        errors.append("timing diagnostics source_tables are not authoritative")
    if not _valid_timestamp(diagnostics.get("generated_at")):
        errors.append("timing diagnostics generated_at is invalid")
    if not isinstance(comparison, Mapping):
        return [*errors, "timing diagnostics comparison artifact is required"]

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

    by_descriptor: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    campaign_pairs: dict[str, set[tuple[str, str]]] = {"A": set(), "B": set()}
    for run in runs:
        run_id = str(run.get("run_id") or "")
        campaign = str(run.get("campaign") or "")
        mode = str(run.get("mode") or "")
        objective_id = str(run.get("objective_id") or "")
        descriptor = (campaign, mode, objective_id)
        if campaign not in campaign_pairs or not mode or not objective_id:
            errors.append(f"timing diagnostics run {run_id} has an invalid descriptor")
        elif descriptor in by_descriptor:
            errors.append(f"timing diagnostics duplicates descriptor {descriptor!r}")
        else:
            by_descriptor[descriptor] = run
            campaign_pairs[campaign].add((mode, objective_id))

        if run.get("state") != "completed":
            errors.append(f"timing diagnostics run {run_id} is not completed")
        started_at = _parse_timestamp(run.get("started_at"))
        completed_at = _parse_timestamp(run.get("completed_at"))
        duration = _number(run.get("duration_ms"))
        if started_at is None or completed_at is None:
            errors.append(f"timing diagnostics run {run_id} lacks valid timestamps")
        elif completed_at < started_at:
            errors.append(f"timing diagnostics run {run_id} has inverted timestamps")
        if duration is None:
            errors.append(f"timing diagnostics run {run_id} lacks valid duration")
        elif started_at is not None and completed_at is not None:
            expected_duration = (completed_at - started_at).total_seconds() * 1000
            if expected_duration < 0 or not math.isclose(
                duration,
                expected_duration,
                abs_tol=1.0,
            ):
                errors.append(
                    f"timing diagnostics run {run_id} duration does not match "
                    "timestamps"
                )
        stages = run.get("semantic_stage_totals")
        if not isinstance(stages, Mapping):
            errors.append(f"timing diagnostics run {run_id} lacks stage totals")
            continue
        for stage_name, stage in stages.items():
            if (
                not isinstance(stage_name, str)
                or not stage_name
                or not isinstance(stage, Mapping)
            ):
                errors.append(f"timing diagnostics run {run_id} has malformed stage")
                continue
            errors.extend(_validate_stage(stage, f"run {run_id} stage {stage_name}"))

    if campaign_pairs["A"] != campaign_pairs["B"]:
        errors.append("timing diagnostics campaign descriptor sets do not match")

    try:
        expected_failures = _comparison_failure_map(comparison)
        tolerances = _tolerance_map(comparison)
    except TimingDiagnosticsError as exc:
        return [*errors, str(exc)]

    raw_failures = diagnostics.get("reproducibility_failures")
    failures = (
        [item for item in raw_failures if isinstance(item, Mapping)]
        if isinstance(raw_failures, list)
        else []
    )
    actual_failure_metrics = [str(item.get("metric") or "") for item in failures]
    if set(actual_failure_metrics) != set(expected_failures) or len(
        actual_failure_metrics
    ) != len(set(actual_failure_metrics)):
        errors.append(
            "timing diagnostics failure set does not match comparison details"
        )

    for failure in failures:
        metric = str(failure.get("metric") or "")
        detail = expected_failures.get(metric)
        if detail is None:
            continue
        if failure.get("detail") != detail:
            errors.append(f"timing failure {metric} detail does not match comparison")
        mode, objective_id, _metric_name = metric.split(".", 2)
        run_a = by_descriptor.get(("A", mode, objective_id))
        run_b = by_descriptor.get(("B", mode, objective_id))
        if run_a is None or run_b is None:
            errors.append(f"timing failure {metric} lacks paired runs")
            continue
        if failure.get("campaign_a_run_id") != run_a.get("run_id"):
            errors.append(f"timing failure {metric} campaign A run ID mismatch")
        if failure.get("campaign_b_run_id") != run_b.get("run_id"):
            errors.append(f"timing failure {metric} campaign B run ID mismatch")

        tolerance = tolerances.get(metric)
        if tolerance is None:
            errors.append(f"timing failure {metric} lacks comparison tolerance")
            continue
        if not _same_number(failure.get("campaign_a_value"), tolerance[1]):
            errors.append(f"timing failure {metric} campaign A value mismatch")
        if not _same_number(failure.get("campaign_b_value"), tolerance[2]):
            errors.append(f"timing failure {metric} campaign B value mismatch")
        expected_ratio = _ratio(_number(tolerance[1]), _number(tolerance[2]))
        if failure.get("value_ratio") != expected_ratio:
            errors.append(f"timing failure {metric} value ratio mismatch")

        stages_a = run_a.get("semantic_stage_totals")
        stages_b = run_b.get("semantic_stage_totals")
        if not isinstance(stages_a, Mapping) or not isinstance(stages_b, Mapping):
            continue
        expected_stages = sorted(set(stages_a) | set(stages_b))
        status = failure.get("semantic_stage_diagnostics_status")
        reason = failure.get("semantic_stage_diagnostics_reason")
        raw_stage_comparisons = failure.get("semantic_stage_latency_comparison")
        stage_comparisons = (
            [item for item in raw_stage_comparisons if isinstance(item, Mapping)]
            if isinstance(raw_stage_comparisons, list)
            else []
        )
        if not expected_stages:
            if status != "not_applicable" or not isinstance(reason, str) or not reason:
                errors.append(f"timing failure {metric} lacks explicit no-stage reason")
            if stage_comparisons:
                errors.append(
                    f"timing failure {metric} has stage comparisons when not applicable"
                )
            continue
        if status != "available" or reason is not None:
            errors.append(
                f"timing failure {metric} stage diagnostics status is invalid"
            )
        actual_stages = [str(item.get("stage") or "") for item in stage_comparisons]
        if actual_stages != expected_stages or len(actual_stages) != len(
            set(actual_stages)
        ):
            errors.append(f"timing failure {metric} stage set mismatch")
            continue

        for item in stage_comparisons:
            stage_name = str(item.get("stage") or "")
            stage_a = stages_a.get(stage_name)
            stage_b = stages_b.get(stage_name)
            present_a = isinstance(stage_a, Mapping)
            present_b = isinstance(stage_b, Mapping)
            if (
                item.get("campaign_a_present") is not present_a
                or item.get("campaign_b_present") is not present_b
            ):
                errors.append(f"timing failure {metric}/{stage_name} presence mismatch")
            if not present_a or not present_b:
                errors.append(
                    f"timing failure {metric}/{stage_name} is missing from a paired run"
                )
                continue
            if (
                stage_a.get("telemetry_complete") is not True
                or stage_b.get("telemetry_complete") is not True
            ):
                errors.append(
                    f"timing failure {metric}/{stage_name} has incomplete telemetry"
                )
            field_pairs = (
                ("campaign_a_attempt_latency_ms", stage_a.get("attempt_latency_ms")),
                ("campaign_b_attempt_latency_ms", stage_b.get("attempt_latency_ms")),
                ("campaign_a_wall_clock_ms", stage_a.get("wall_clock_ms")),
                ("campaign_b_wall_clock_ms", stage_b.get("wall_clock_ms")),
                ("campaign_a_telemetry_complete", stage_a.get("telemetry_complete")),
                ("campaign_b_telemetry_complete", stage_b.get("telemetry_complete")),
                ("campaign_a_status_counts", stage_a.get("status_counts")),
                ("campaign_b_status_counts", stage_b.get("status_counts")),
                ("campaign_a_attempt_count", stage_a.get("attempt_count")),
                ("campaign_b_attempt_count", stage_b.get("attempt_count")),
                (
                    "campaign_a_missing_attempt_latency_count",
                    stage_a.get("missing_attempt_latency_count"),
                ),
                (
                    "campaign_b_missing_attempt_latency_count",
                    stage_b.get("missing_attempt_latency_count"),
                ),
                (
                    "campaign_a_calls_missing_attempt_metadata",
                    stage_a.get("calls_missing_attempt_metadata"),
                ),
                (
                    "campaign_b_calls_missing_attempt_metadata",
                    stage_b.get("calls_missing_attempt_metadata"),
                ),
                ("campaign_a_retry_count", stage_a.get("retry_count")),
                ("campaign_b_retry_count", stage_b.get("retry_count")),
                (
                    "campaign_a_missing_wall_clock_count",
                    stage_a.get("missing_wall_clock_count"),
                ),
                (
                    "campaign_b_missing_wall_clock_count",
                    stage_b.get("missing_wall_clock_count"),
                ),
            )
            for field, expected in field_pairs:
                if item.get(field) != expected:
                    errors.append(
                        f"timing failure {metric}/{stage_name} {field} mismatch"
                    )
            if item.get("attempt_latency_ratio") != _ratio(
                stage_a.get("attempt_latency_ms"), stage_b.get("attempt_latency_ms")
            ):
                errors.append(
                    f"timing failure {metric}/{stage_name} attempt ratio mismatch"
                )
            if item.get("wall_clock_ratio") != _ratio(
                stage_a.get("wall_clock_ms"), stage_b.get("wall_clock_ms")
            ):
                errors.append(
                    f"timing failure {metric}/{stage_name} wall-clock ratio mismatch"
                )
    return errors
