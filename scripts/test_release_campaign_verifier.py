"""Contract tests for authoritative full-campaign verification."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.release_benchmark import (
    MANDATORY_PERFORMANCE_METRICS,
    MANDATORY_QUALITY_METRICS,
    ReleaseBenchmarkConfig,
)
from verify_release_campaign import (
    EXPECTED_MODES,
    EXPECTED_RUNS_PER_CAMPAIGN,
    EXPECTED_TOTAL_RUNS,
    _validate_run,
)


def _metric(name: str, run_id: str, *, status: str = "measured", value=1.0):
    return {
        "name": name,
        "value": value,
        "status": status,
        "formula": f"authoritative formula for {name}",
        "source": {
            "table": "authoritative_table",
            "column": name,
            "run_id": run_id,
            "method": "measured",
            "record_ids": [str(uuid4())],
        },
    }


def _run(mode: str):
    run_id = str(uuid4())
    quality = [_metric(name, run_id) for name in sorted(MANDATORY_QUALITY_METRICS)]
    performance = [
        _metric(
            name,
            run_id,
            status=(
                "not_applicable"
                if mode == "deterministic_debug" and name == "total_tokens"
                else "measured"
            ),
            value=(
                0
                if mode == "deterministic_debug" and name == "total_tokens"
                else 1.0
            ),
        )
        for name in sorted(MANDATORY_PERFORMANCE_METRICS)
    ]
    performance.extend(
        [
            _metric("total_latency_ms", run_id),
            _metric("semantic_calls", run_id),
        ]
    )
    return {
        "run_id": run_id,
        "mode": mode,
        "objective_id": "obj-001",
        "errors": [],
        "quality_metrics": quality,
        "performance_metrics": performance,
        "integrity_checks": [
            {"check": name, "passed": True, "details": "ok"}
            for name in ReleaseBenchmarkConfig().integrity_checks
        ],
    }


def test_authoritative_campaign_shape_is_two_modes_by_five_objectives():
    assert EXPECTED_MODES == ("autonomous_local", "deterministic_debug")
    assert EXPECTED_RUNS_PER_CAMPAIGN == 10
    assert EXPECTED_TOTAL_RUNS == 20


def test_deterministic_total_tokens_may_be_mode_scoped_not_applicable():
    run = _run("deterministic_debug")
    errors, run_id, pair = _validate_run(
        run, set(ReleaseBenchmarkConfig().integrity_checks)
    )
    assert errors == []
    assert run_id == run["run_id"]
    assert pair == ("deterministic_debug", "obj-001")


def test_autonomous_total_tokens_must_be_measured():
    run = _run("autonomous_local")
    token_metric = next(
        item for item in run["performance_metrics"] if item["name"] == "total_tokens"
    )
    token_metric["status"] = "not_applicable"
    token_metric["value"] = 0
    errors, _run_id, _pair = _validate_run(
        run, set(ReleaseBenchmarkConfig().integrity_checks)
    )
    assert any("unexpectedly not_applicable" in item for item in errors)


def test_agent_led_is_not_part_of_authoritative_campaign():
    run = _run("agent_led")
    errors, _run_id, _pair = _validate_run(
        run, set(ReleaseBenchmarkConfig().integrity_checks)
    )
    assert any("unexpected execution mode" in item for item in errors)


def test_release_workflow_binds_candidate_and_blocks_agent_led():
    workflow = (
        SCRIPTS.parent / ".github" / "workflows" / "release-campaign.yml"
    ).read_text(encoding="utf-8")
    assert '--candidate-sha "$CANDIDATE_SHA"' in workflow
    assert 'SMOKE_DISABLE_AGENT_LED: "1"' in workflow
    assert "verify_release_campaign.py" in workflow
    assert "retention-days: 90" in workflow
