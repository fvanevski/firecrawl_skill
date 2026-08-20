"""Behavioral contract tests for authoritative full-campaign dispatch."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from run_release_campaign import AUTHORITATIVE_MODES, normalize_mode_metadata
from verify_release_campaign import (
    WorkflowIdentity,
    validate_metric_record,
    validate_reproducibility,
    validate_run_shape,
    validate_workflow_identity,
)

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
WORKFLOW = SCRIPTS.parent / ".github" / "workflows" / "release-campaign.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _identity(**overrides: str) -> WorkflowIdentity:
    sha = "a" * 40
    values = {
        "candidate_sha": sha,
        "dispatch_sha": sha,
        "workflow_sha": sha,
        "dispatch_ref": "refs/heads/main",
        "repository": "fvanevski/firecrawl_skill",
        "run_id": "123",
        "run_attempt": "1",
        "workflow_ref": "fvanevski/firecrawl_skill/.github/workflows/release-campaign.yml@refs/heads/main",
    }
    values.update(overrides)
    return WorkflowIdentity(**values)


def _source(run_id: str, **overrides):
    value = {
        "table": "metric_events",
        "column": "value",
        "run_id": run_id,
        "method": "authoritative-v1",
        "record_ids": ["record-1"],
        "stages": [],
        "stage_set_version": "",
        "sample_count": 1,
        "device_type": "",
        "device_index": None,
        "device_uuid": "",
        "collector": "collector",
        "collector_version": "1",
        "status_counts": {"measured": 1},
    }
    value.update(overrides)
    return value


def _metric(name: str, run_id: str, **overrides):
    value = {
        "name": name,
        "value": 1.0,
        "status": "measured",
        "formula": "authoritative measurement",
        "source": _source(run_id),
    }
    value.update(overrides)
    return value


def _runs():
    objectives = [f"obj-{index}" for index in range(5)]
    values = []
    counter = 1
    for mode in AUTHORITATIVE_MODES:
        for objective in objectives:
            values.append(
                {
                    "mode": mode,
                    "objective_id": objective,
                    "run_id": str(UUID(int=counter)),
                }
            )
            counter += 1
    return objectives, values


def test_workflow_binds_dispatch_workflow_and_checkout_to_candidate():
    workflow = _workflow_text()
    assert "DISPATCH_SHA: ${{ github.sha }}" in workflow
    assert "WORKFLOW_SHA: ${{ github.workflow_sha }}" in workflow
    assert "DISPATCH_REF: ${{ github.ref }}" in workflow
    assert 'test "$DISPATCH_SHA" = "$CANDIDATE_SHA"' in workflow
    assert 'test "$WORKFLOW_SHA" = "$CANDIDATE_SHA"' in workflow
    assert 'test "$DISPATCH_REF" = "refs/heads/main"' in workflow
    assert 'test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"' in workflow


def test_workflow_uses_standalone_runner_and_strict_verifier():
    workflow = _workflow_text()
    assert "python scripts/run_release_campaign.py" in workflow
    assert "python scripts/verify_release_campaign_strict.py" in workflow
    assert "python scripts/verify_release_campaign.py" not in workflow
    assert "python - <<'PY'" not in workflow
    assert "retention-days: 90" in workflow
    assert "if: always()" in workflow
    assert "steps.execute.outcome" in workflow
    assert "steps.verify.outcome" in workflow


def test_identity_accepts_only_exact_main_head():
    assert not validate_workflow_identity(
        _identity(),
        checkout_sha="a" * 40,
        tree_hash="b" * 40,
        working_tree_clean=True,
    )
    errors = validate_workflow_identity(
        _identity(workflow_sha="c" * 40, dispatch_ref="refs/heads/other"),
        checkout_sha="a" * 40,
        tree_hash="b" * 40,
        working_tree_clean=False,
    )
    assert any("identity mismatch" in item for item in errors)
    assert any("refs/heads/main" in item for item in errors)
    assert any("not clean" in item for item in errors)


def test_run_shape_requires_exact_two_by_five_contract():
    objectives, runs = _runs()
    assert not validate_run_shape(runs, objective_ids=objectives)

    fifteen = [*runs]
    for index in range(5):
        fifteen.append(
            {
                "mode": "agent_led",
                "objective_id": objectives[index],
                "run_id": str(UUID(int=100 + index)),
            }
        )
    errors = validate_run_shape(fifteen, objective_ids=objectives)
    assert any("expected 10 runs" in item for item in errors)
    assert any("run set mismatch" in item for item in errors)


def test_run_shape_rejects_duplicate_uuid():
    objectives, runs = _runs()
    runs[-1]["run_id"] = runs[0]["run_id"]
    errors = validate_run_shape(runs, objective_ids=objectives)
    assert any("not unique" in item for item in errors)


def test_measured_metric_requires_substantive_provenance():
    run_id = str(UUID(int=1))
    assert not validate_metric_record(
        _metric("candidate_recall", run_id),
        mode="autonomous_local",
        run_id=run_id,
        quality=True,
    )
    invalid = _metric(
        "candidate_recall",
        run_id,
        source={"run_id": run_id},
    )
    errors = validate_metric_record(
        invalid,
        mode="autonomous_local",
        run_id=run_id,
        quality=True,
    )
    assert any("lacks table" in item for item in errors)
    assert any("no authoritative records or samples" in item for item in errors)


def test_only_deterministic_tokens_may_be_not_applicable():
    run_id = str(UUID(int=1))
    token_na = _metric(
        "total_tokens",
        run_id,
        value=None,
        status="not_applicable",
        source=_source(
            run_id,
            record_ids=[],
            sample_count=0,
            status_counts={"not_invoked": 1},
        ),
    )
    assert not validate_metric_record(
        token_na,
        mode="deterministic_debug",
        run_id=run_id,
        quality=False,
    )
    errors = validate_metric_record(
        token_na,
        mode="autonomous_local",
        run_id=run_id,
        quality=False,
    )
    assert any("unexpectedly not_applicable" in item for item in errors)


def test_resource_metrics_require_complete_sampling_provenance():
    run_id = str(UUID(int=1))
    cpu = _metric(
        "cpu_percent",
        run_id,
        source=_source(run_id, record_ids=[], sample_count=1),
    )
    gpu = _metric(
        "gpu_memory_mb",
        run_id,
        source=_source(run_id, record_ids=[], sample_count=2, device_uuid=""),
    )
    cpu_errors = validate_metric_record(
        cpu,
        mode="autonomous_local",
        run_id=run_id,
        quality=False,
    )
    gpu_errors = validate_metric_record(
        gpu,
        mode="autonomous_local",
        run_id=run_id,
        quality=False,
    )
    assert any("at least two samples" in item for item in cpu_errors)
    assert any("at least three samples" in item for item in gpu_errors)
    assert any("device_uuid" in item for item in gpu_errors)


def test_reproducibility_requires_pass_policy_and_tolerances():
    valid = {
        "all_within_tolerance": True,
        "details": [],
        "policy_version": "reproducibility-policy-v2",
        "relative_tolerance": 0.15,
        "operational_ratio_limit": 2.0,
    }
    assert not validate_reproducibility(valid)
    invalid = {"all_within_tolerance": False, "details": ["drift"]}
    errors = validate_reproducibility(invalid)
    assert any("did not pass" in item for item in errors)
    assert any("policy version" in item for item in errors)


def test_runner_normalizes_raw_mode_metadata(tmp_path: Path):
    result_dir = tmp_path / "A" / "20260731T000000Z"
    result_dir.mkdir(parents=True)
    (result_dir / "result.json").write_text("{}\n", encoding="utf-8")
    (result_dir / "environment.json").write_text(
        json.dumps({"execution_modes": ["agent_led", *AUTHORITATIVE_MODES]}),
        encoding="utf-8",
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "modes": ["agent_led", *AUTHORITATIVE_MODES],
                "campaign_a": {"result_path": str(result_dir)},
                "campaign_b": {},
            }
        ),
        encoding="utf-8",
    )

    normalize_mode_metadata(tmp_path)

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    environment = json.loads(
        (result_dir / "environment.json").read_text(encoding="utf-8")
    )
    assert manifest["modes"] == list(AUTHORITATIVE_MODES)
    assert environment["execution_modes"] == list(AUTHORITATIVE_MODES)
