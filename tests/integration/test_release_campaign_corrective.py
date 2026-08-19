"""Focused regressions for the RC-10 credentialed campaign correction."""

from __future__ import annotations

import copy
import itertools
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from enforce_release_campaign_outcomes import main as enforce_outcomes
from run_release_campaign import write_timing_diagnostics
from verify_corrective_junit import main as verify_junit
from verify_release_campaign import _database_completion
from verify_release_campaign_strict import validate_timing_diagnostics

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
ROOT = SCRIPTS.parent
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release-campaign.yml"
CORRECTIVE_WORKFLOW = ROOT / ".github" / "workflows" / "rc10-corrective.yml"
TIMING_DOC = ROOT / "references" / "release-campaign-timing-diagnostics.md"


def _stage(*, call_id: str, key: str, attempt_ms: float, wall_ms: float) -> dict:
    return {
        "call_count": 1,
        "call_ids": [call_id],
        "idempotency_keys": [key],
        "status_counts": {"complete": 1},
        "attempt_count": 1,
        "attempt_latency_observation_count": 1,
        "missing_attempt_latency_count": 0,
        "calls_missing_attempt_metadata": 0,
        "retry_count": 0,
        "attempt_latency_ms": attempt_ms,
        "wall_clock_observation_count": 1,
        "missing_wall_clock_count": 0,
        "wall_clock_ms": wall_ms,
        "telemetry_complete": True,
    }


def _valid_contract() -> tuple[dict, dict, list[str]]:
    run_a = str(UUID(int=1))
    run_b = str(UUID(int=2))
    call_a = str(UUID(int=11))
    call_b = str(UUID(int=12))
    metric = "autonomous_local.obj-003.total_latency_ms"
    detail = f"{metric}: 75000.0000 vs 10000.0000 (ratio 7.5000 > 2.0)"
    stage_a = _stage(
        call_id=call_a,
        key="timing-a",
        attempt_ms=19869.0,
        wall_ms=20000.0,
    )
    stage_b = _stage(
        call_id=call_b,
        key="timing-b",
        attempt_ms=782.0,
        wall_ms=800.0,
    )
    comparison = {
        "details": [detail],
        "quality_tolerances": [],
        "performance_tolerances": [[metric, 75000.0, 10000.0, 0.8667]],
    }
    diagnostics = {
        "schema_version": "release-campaign-timing-v2",
        "candidate_sha": "a" * 40,
        "generated_at": "2026-08-03T20:00:00+00:00",
        "source_tables": ["research_runs", "semantic_calls"],
        "run_count": 2,
        "runs": [
            {
                "campaign": "A",
                "mode": "autonomous_local",
                "objective_id": "obj-003",
                "run_id": run_a,
                "state": "completed",
                "started_at": "2026-08-03T19:58:45+00:00",
                "completed_at": "2026-08-03T20:00:00+00:00",
                "duration_ms": 75000.0,
                "semantic_stage_totals": {"claim_extraction": stage_a},
            },
            {
                "campaign": "B",
                "mode": "autonomous_local",
                "objective_id": "obj-003",
                "run_id": run_b,
                "state": "completed",
                "started_at": "2026-08-03T19:59:50+00:00",
                "completed_at": "2026-08-03T20:00:00+00:00",
                "duration_ms": 10000.0,
                "semantic_stage_totals": {"claim_extraction": stage_b},
            },
        ],
        "reproducibility_failures": [
            {
                "metric": metric,
                "detail": detail,
                "campaign_a_run_id": run_a,
                "campaign_b_run_id": run_b,
                "campaign_a_value": 75000.0,
                "campaign_b_value": 10000.0,
                "value_ratio": 7.5,
                "semantic_stage_diagnostics_status": "available",
                "semantic_stage_diagnostics_reason": None,
                "semantic_stage_latency_comparison": [
                    {
                        "stage": "claim_extraction",
                        "campaign_a_present": True,
                        "campaign_b_present": True,
                        "campaign_a_attempt_latency_ms": 19869.0,
                        "campaign_b_attempt_latency_ms": 782.0,
                        "attempt_latency_ratio": 25.4079,
                        "campaign_a_wall_clock_ms": 20000.0,
                        "campaign_b_wall_clock_ms": 800.0,
                        "wall_clock_ratio": 25.0,
                        "campaign_a_telemetry_complete": True,
                        "campaign_b_telemetry_complete": True,
                        "campaign_a_status_counts": {"complete": 1},
                        "campaign_b_status_counts": {"complete": 1},
                        "campaign_a_attempt_count": 1,
                        "campaign_b_attempt_count": 1,
                        "campaign_a_missing_attempt_latency_count": 0,
                        "campaign_b_missing_attempt_latency_count": 0,
                        "campaign_a_calls_missing_attempt_metadata": 0,
                        "campaign_b_calls_missing_attempt_metadata": 0,
                        "campaign_a_retry_count": 0,
                        "campaign_b_retry_count": 0,
                        "campaign_a_missing_wall_clock_count": 0,
                        "campaign_b_missing_wall_clock_count": 0,
                    }
                ],
            }
        ],
    }
    return diagnostics, comparison, [run_a, run_b]


@pytest.mark.parametrize(
    ("execute", "verify", "upload"),
    tuple(itertools.product(("success", "failure"), repeat=3)),
)
def test_gate_enforcer_reports_before_enforcing(
    execute: str,
    verify: str,
    upload: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    summary = tmp_path / "summary.md"
    result = enforce_outcomes(
        [
            "--execute",
            execute,
            "--verify",
            verify,
            "--upload",
            upload,
            "--summary-file",
            str(summary),
        ]
    )
    captured = capsys.readouterr()
    assert f"execute={execute}" in captured.out
    assert f"verify={verify}" in captured.out
    assert f"upload={upload}" in captured.out
    assert "Campaign execution outcome" in captured.out
    assert "Campaign verification outcome" in captured.out
    assert "Campaign artifact upload outcome" in captured.out
    expected = 0 if {execute, verify, upload} == {"success"} else 1
    assert result == expected
    assert "| execute |" in summary.read_text(encoding="utf-8")
    labels = {
        "execute": "Campaign execution",
        "verify": "Campaign verification",
        "upload": "Campaign artifact upload",
    }
    for key, value in (("execute", execute), ("verify", verify), ("upload", upload)):
        if value != "success":
            assert (
                f"::error title={labels[key]} failed::outcome={value}" in captured.err
            )


def test_release_workflow_uses_executable_strict_gate_contracts():
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "python scripts/verify_release_campaign_strict.py" in workflow
    assert "python scripts/enforce_release_campaign_outcomes.py" in workflow
    assert '--execute "${{ steps.execute.outcome }}"' in workflow
    assert '--verify "${{ steps.verify.outcome }}"' in workflow
    assert '--upload "${{ steps.upload.outcome }}"' in workflow
    assert "gate_failed=" not in workflow


def test_corrective_matrix_collects_database_regression_on_both_versions():
    workflow = CORRECTIVE_WORKFLOW.read_text(encoding="utf-8")
    assert 'python-version: ["3.11", "3.12"]' in workflow
    assert "tests/integration/test_release_campaign_corrective.py" in workflow
    assert "RESEARCH_STORE_TEST_DATABASE_URL" in workflow
    assert "verify_corrective_junit.py" in workflow
    assert "test_current_schema_completion_and_stage_timing_diagnostics" in workflow


def test_junit_verifier_rejects_missing_or_skipped_corrective_case(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    passed = tmp_path / "passed.xml"
    passed.write_text(
        '<testsuite><testcase classname="x" '
        'name="test_current_schema_completion_and_stage_timing_diagnostics"/>'
        "</testsuite>",
        encoding="utf-8",
    )
    assert (
        verify_junit(
            [
                "--junitxml",
                str(passed),
                "--test-name",
                "test_current_schema_completion_and_stage_timing_diagnostics",
            ]
        )
        == 0
    )

    skipped = tmp_path / "skipped.xml"
    skipped.write_text(
        '<testsuite><testcase classname="x" '
        'name="test_current_schema_completion_and_stage_timing_diagnostics">'
        '<skipped message="no database"/></testcase></testsuite>',
        encoding="utf-8",
    )
    assert (
        verify_junit(
            [
                "--junitxml",
                str(skipped),
                "--test-name",
                "test_current_schema_completion_and_stage_timing_diagnostics",
            ]
        )
        == 1
    )
    assert "did not pass" in capsys.readouterr().err


def test_timing_diagnostics_cross_validate_comparison_and_provenance():
    diagnostics, comparison, run_ids = _valid_contract()
    assert not validate_timing_diagnostics(
        diagnostics,
        candidate_sha="a" * 40,
        run_ids=run_ids,
        comparison=comparison,
    )


def test_timing_diagnostics_reject_silent_evidence_downgrades():
    diagnostics, comparison, run_ids = _valid_contract()

    def missing_sources(value: dict) -> None:
        value["source_tables"] = []

    def missing_failure(value: dict) -> None:
        value["reproducibility_failures"] = []

    def missing_run_binding(value: dict) -> None:
        value["reproducibility_failures"][0]["campaign_a_run_id"] = None

    def changed_value(value: dict) -> None:
        value["reproducibility_failures"][0]["campaign_a_value"] = 1.0

    def missing_stage_totals(value: dict) -> None:
        value["runs"][0]["semantic_stage_totals"] = {}

    def incomplete_stage(value: dict) -> None:
        stage = value["runs"][0]["semantic_stage_totals"]["claim_extraction"]
        stage["missing_attempt_latency_count"] = 1
        stage["attempt_latency_observation_count"] = 0
        stage["attempt_latency_ms"] = None
        stage["telemetry_complete"] = False
        failure = value["reproducibility_failures"][0][
            "semantic_stage_latency_comparison"
        ][0]
        failure["campaign_a_attempt_latency_ms"] = None
        failure["campaign_a_telemetry_complete"] = False
        failure["campaign_a_missing_attempt_latency_count"] = 1
        failure["attempt_latency_ratio"] = None

    cases = (
        (missing_sources, "source_tables"),
        (missing_failure, "failure set"),
        (missing_run_binding, "run ID mismatch"),
        (changed_value, "value mismatch"),
        (missing_stage_totals, "missing from a paired run"),
        (incomplete_stage, "incomplete telemetry"),
    )
    for mutate, expected in cases:
        changed = copy.deepcopy(diagnostics)
        mutate(changed)
        errors = validate_timing_diagnostics(
            changed,
            candidate_sha="a" * 40,
            run_ids=run_ids,
            comparison=comparison,
        )
        assert any(expected in item for item in errors), (expected, errors)


def test_timing_contract_documentation_preserves_target_a_boundaries():
    text = TIMING_DOC.read_text(encoding="utf-8")
    for required in (
        "release-campaign-timing-v2",
        "PostgreSQL remains authoritative",
        "not runtime state",
        "Missing timing is never represented as zero",
        "Failed and retried semantic calls",
        "comparison.json",
        "BLOB_ROOT",
        "Qdrant",
        "Valkey",
    ):
        assert required in text


@pytest.mark.skipif(
    not os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL"),
    reason="requires explicit disposable PostgreSQL test DSN",
)
def test_current_schema_completion_and_stage_timing_diagnostics(tmp_path: Path):
    """Exercise completion, retries, failures, and timing on the current schema."""
    from research_store.postgres import connect, migrate

    database_url = os.environ["RESEARCH_STORE_TEST_DATABASE_URL"]
    migrate(database_url)
    candidate_sha = "c" * 40
    run_a = uuid4()
    run_b = uuid4()
    call_a_failed = uuid4()
    call_a_complete = uuid4()
    call_b_complete = uuid4()

    try:
        with connect(database_url) as connection, connection.cursor() as cursor:
            # Build synthetic semantic telemetry while the runs are nonterminal.
            # The terminal-provenance guard intentionally forbids adding semantic
            # calls after a run is completed. Keep the prior exact timestamps so
            # timing diagnostics remain deterministic, then mark the runs completed
            # only after all semantic provenance has been inserted.
            cursor.execute(
                """INSERT INTO research_runs(
                       id, objective, state, execution_mode,
                       started_at, completed_at
                   ) VALUES
                       (%s, 'timing A', 'validating', 'autonomous_local',
                        now() - interval '75 seconds', now()),
                       (%s, 'timing B', 'validating', 'autonomous_local',
                        now() - interval '10 seconds', now())""",
                (run_a, run_b),
            )
            cursor.execute(
                """INSERT INTO semantic_calls(
                       id, run_id, stage, provider, model, prompt_version,
                       input_sha256, response_metadata, status,
                       idempotency_key, started_at, completed_at
                   ) VALUES
                       (%s, %s, 'claim_extraction', 'local', 'chat', 'test-v1',
                        %s, %s::jsonb, 'failed', %s,
                        now() - interval '5 seconds', now()),
                       (%s, %s, 'claim_extraction', 'local', 'chat', 'test-v1',
                        %s, %s::jsonb, 'complete', %s,
                        now() - interval '20 seconds', now()),
                       (%s, %s, 'claim_extraction', 'local', 'chat', 'test-v1',
                        %s, %s::jsonb, 'complete', %s,
                        now() - interval '0.8 seconds', now())""",
                (
                    call_a_failed,
                    run_a,
                    "a" * 64,
                    json.dumps({"attempts": [{"attempt": 1, "latency_ms": 5000}]}),
                    f"timing-a-failed-{run_a}",
                    call_a_complete,
                    run_a,
                    "b" * 64,
                    json.dumps(
                        {
                            "attempts": [
                                {"attempt": 1, "latency_ms": 8000},
                                {"attempt": 2, "latency_ms": 12000},
                            ]
                        }
                    ),
                    f"timing-a-complete-{run_a}",
                    call_b_complete,
                    run_b,
                    "c" * 64,
                    json.dumps({"attempts": [{"attempt": 1, "latency_ms": 782}]}),
                    f"timing-b-complete-{run_b}",
                ),
            )
            cursor.execute(
                "UPDATE research_runs SET state='completed' WHERE id=ANY(%s::uuid[])",
                ([run_a, run_b],),
            )

        result_paths: dict[str, Path] = {}
        for label, run_id in (("A", run_a), ("B", run_b)):
            result_dir = tmp_path / label / "20260803T000000Z"
            result_dir.mkdir(parents=True)
            result_dir.joinpath("result.json").write_text(
                json.dumps(
                    {
                        "campaign_id": f"campaign-{label.lower()}",
                        "runs": [
                            {
                                "mode": "autonomous_local",
                                "objective_id": "obj-003",
                                "run_id": str(run_id),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result_paths[label] = result_dir

        tmp_path.joinpath("manifest.json").write_text(
            json.dumps(
                {
                    "candidate_sha": candidate_sha,
                    "campaign_a": {"result_path": str(result_paths["A"])},
                    "campaign_b": {"result_path": str(result_paths["B"])},
                }
            ),
            encoding="utf-8",
        )
        comparison = {
            "details": [
                (
                    "autonomous_local.obj-003.total_latency_ms: "
                    "75000.0000 vs 10000.0000 (ratio 7.5000 > 2.0)"
                )
            ],
            "quality_tolerances": [],
            "performance_tolerances": [
                [
                    "autonomous_local.obj-003.total_latency_ms",
                    75000.0,
                    10000.0,
                    0.8667,
                ]
            ],
        }
        comparison_dir = tmp_path / "reproducibility" / "20260803T000000Z"
        comparison_dir.mkdir(parents=True)
        comparison_dir.joinpath("comparison.json").write_text(
            json.dumps(comparison),
            encoding="utf-8",
        )

        database_runs, database_errors = _database_completion(
            database_url,
            [str(run_a), str(run_b)],
        )
        assert database_errors == []
        assert database_runs[str(run_a)]["state"] == "completed"
        assert database_runs[str(run_a)]["status"] == "completed"
        assert (
            database_runs[str(run_a)]["orchestration_outcome_source"]
            == "research_runs.state"
        )

        diagnostics = write_timing_diagnostics(tmp_path, database_url)
        assert diagnostics["run_count"] == 2
        assert not validate_timing_diagnostics(
            diagnostics,
            candidate_sha=candidate_sha,
            run_ids=[str(run_a), str(run_b)],
            comparison=comparison,
        )
        run_a_evidence = next(
            item for item in diagnostics["runs"] if item["run_id"] == str(run_a)
        )
        totals = run_a_evidence["semantic_stage_totals"]["claim_extraction"]
        assert totals["call_count"] == 2
        assert totals["status_counts"] == {"complete": 1, "failed": 1}
        assert totals["attempt_count"] == 3
        assert totals["retry_count"] == 1
        assert totals["attempt_latency_ms"] == 25000.0
        assert totals["telemetry_complete"] is True
        assert set(totals["call_ids"]) == {
            str(call_a_failed),
            str(call_a_complete),
        }

        failure = diagnostics["reproducibility_failures"][0]
        assert failure["campaign_a_run_id"] == str(run_a)
        assert failure["campaign_b_run_id"] == str(run_b)
        stage = failure["semantic_stage_latency_comparison"][0]
        assert stage["stage"] == "claim_extraction"
        assert stage["campaign_a_attempt_latency_ms"] == 25000.0
        assert stage["campaign_b_attempt_latency_ms"] == 782.0
        assert stage["attempt_latency_ratio"] > 30
        assert stage["campaign_a_status_counts"] == {"complete": 1, "failed": 1}
    finally:
        with connect(database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE research_runs SET state='validating' WHERE id=ANY(%s::uuid[])",
                ([run_a, run_b],),
            )
            cursor.execute(
                "DELETE FROM semantic_calls WHERE run_id = ANY(%s::uuid[])",
                ([run_a, run_b],),
            )
            cursor.execute(
                "DELETE FROM research_runs WHERE id = ANY(%s::uuid[])",
                ([run_a, run_b],),
            )
