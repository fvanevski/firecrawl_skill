from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from firecrawl_skill.research_store.research_controller_cli import build_parser

ROOT = Path(__file__).resolve().parents[2]
FRESEARCH = ROOT / "scripts" / "fresearch"
CONTROLLER = (
    ROOT / "src" / "firecrawl_skill" / "research_store" / "research_controller.py"
)
RETAINED = (
    ROOT / "src" / "firecrawl_skill" / "research_store" / "retained_review_service.py"
)
SCHEMA_ROOT = ROOT / "schemas" / "research-workflow"


def test_fresearch_is_thin_and_does_not_orchestrate_sibling_clis() -> None:
    wrapper = FRESEARCH.read_text(encoding="utf-8")
    controller = CONTROLLER.read_text(encoding="utf-8")
    assert "research_controller_cli" in wrapper
    assert "subprocess" not in controller
    for forbidden in (
        "frun prepare",
        "frun seal-acquisition",
        "frun resume",
        "frun synthesize",
        "scripts/fsearch_smart",
        "scripts/fscrape",
        "research-db",
    ):
        assert forbidden not in controller


def test_fresearch_shim_executes_module_entrypoint() -> None:
    env = dict(os.environ)
    env["FIRECRAWL_RESEARCH_AUTO_ENV"] = "0"

    completed = subprocess.run(
        [str(FRESEARCH), "--help"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "usage:" in completed.stdout
    for command in ("run", "continue", "status", "result"):
        assert command in completed.stdout


def test_fresearch_public_surface_uses_high_level_runs_and_operator_actions() -> None:
    parser = build_parser()
    run_id = "fr_00000000000000000000000000000001"
    action_id = "oa_00000000000000000000000000000001"
    for command in ("continue", "status", "result"):
        assert parser.parse_args([command, run_id]).command == command
    for command in ("action",):
        assert parser.parse_args([command, action_id]).command == command
    assert parser.parse_args(["run", "research", "objective"]).command == "run"
    assert (
        parser.parse_args(
            ["run", "--delivery-mode", "host_handoff", "research objective"]
        ).delivery_mode
        == "host_handoff"
    )
    assert (
        parser.parse_args(
            ["approve", action_id, "--reason", "ok", "--authorized-by", "human"]
        ).command
        == "approve"
    )
    assert (
        parser.parse_args(
            [
                "fork",
                action_id,
                "revised objective",
                "--reason",
                "scope changed",
                "--authorized-by",
                "human",
            ]
        ).command
        == "fork"
    )

    parsed = parser.parse_args(["continue", run_id])
    assert vars(parsed) == {"command": "continue", "run_id": run_id}
    with pytest.raises(SystemExit):
        parser.parse_args(["prepare", run_id])
    with pytest.raises(SystemExit):
        parser.parse_args(["continue", run_id, "--revision", "3"])


def test_machine_contract_schemas_require_public_identity_and_result_flags() -> None:
    directive = json.loads(
        (SCHEMA_ROOT / "workflow-directive-v2.json").read_text(encoding="utf-8")
    )
    result = json.loads(
        (SCHEMA_ROOT / "research-result-v3.json").read_text(encoding="utf-8")
    )
    assert directive["properties"]["schema_version"]["const"] == (
        "workflow-directive-v2"
    )
    assert result["properties"]["schema_version"]["const"] == "research-result-v3"
    assert directive["properties"]["run_id"]["pattern"].startswith("^fr_")
    assert result["properties"]["run_id"]["pattern"].startswith("^fr_")
    for field in ("result_ready", "handoff_ready", "objective_satisfied"):
        assert field in directive["required"]
        assert field in result["required"]
    assert result["properties"]["handoff"]["anyOf"][1]["$ref"] == (
        "research-handoff-v1.json"
    )


def test_retained_review_uses_postgres_and_persisted_temporal_clock() -> None:
    source = RETAINED.read_text(encoding="utf-8")
    assert 'requested_mode="lexical"' in source
    assert '"qdrant_authoritative": False' in source
    assert "passage_temporally_qualifies" in source
    assert "now=evaluated_at" in source
    assert 'role="retained"' in source
