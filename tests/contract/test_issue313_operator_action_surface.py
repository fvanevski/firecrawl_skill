from __future__ import annotations

import json
from pathlib import Path

import pytest

from firecrawl_skill.research_store.research_controller_cli import build_parser

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = ROOT / "schemas" / "research-workflow"

RUN_ID = "fr_00000000000000000000000000000001"
ACTION_ID = "oa_00000000000000000000000000000001"
SUBJECT_ID = "00000000-0000-0000-0000-000000000001"


def test_fresearch_operator_surface_accepts_only_high_level_human_inputs() -> None:
    parser = build_parser()

    approve = vars(
        parser.parse_args(
            [
                "approve",
                ACTION_ID,
                "--reason",
                "human approval",
                "--authorized-by",
                "operator@example.test",
            ]
        )
    )
    assert approve == {
        "command": "approve",
        "action_id": ACTION_ID,
        "reason": "human approval",
        "authorized_by": "operator@example.test",
    }

    curate = vars(
        parser.parse_args(
            [
                "curate",
                ACTION_ID,
                "--retain",
                SUBJECT_ID,
                "--reject-rest",
                "--reason",
                "curated evidence",
                "--authorized-by",
                "operator@example.test",
            ]
        )
    )
    assert curate == {
        "command": "curate",
        "action_id": ACTION_ID,
        "retain": [SUBJECT_ID],
        "reject_rest": True,
        "reason": "curated evidence",
        "authorized_by": "operator@example.test",
    }

    fork = vars(
        parser.parse_args(
            [
                "fork",
                ACTION_ID,
                "revised",
                "objective",
                "--reason",
                "human scope change",
                "--authorized-by",
                "operator@example.test",
            ]
        )
    )
    assert fork == {
        "command": "fork",
        "action_id": ACTION_ID,
        "revised_objective": ["revised", "objective"],
        "reason": "human scope change",
        "authorized_by": "operator@example.test",
    }

    forbidden_generated_parameters = (
        "--check-id",
        "--limit-name",
        "--lifecycle-revision",
        "--fingerprint",
        "--research-spec-id",
    )
    for flag in forbidden_generated_parameters:
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "approve",
                    ACTION_ID,
                    "--reason",
                    "approval",
                    "--authorized-by",
                    "operator@example.test",
                    flag,
                    "generated-value",
                ]
            )


def test_fresearch_operator_surface_preserves_public_run_and_action_identities() -> None:
    parser = build_parser()
    assert parser.parse_args(["continue", RUN_ID]).run_id == RUN_ID
    assert parser.parse_args(["action", ACTION_ID]).action_id == ACTION_ID
    curated = parser.parse_args(["run", "objective", "--curated"])
    assert curated.curated is True


def test_public_action_identity_is_canonical_lowercase() -> None:
    from firecrawl_skill.research_store.operator_action_service import (
        validate_public_action_id,
    )

    assert validate_public_action_id(ACTION_ID) == ACTION_ID
    with pytest.raises(ValueError, match="public oa_<uuid>"):
        validate_public_action_id("oa_A0000000000000000000000000000001")


def test_operator_action_schema_exposes_no_generated_internal_authority() -> None:
    schema = json.loads(
        (SCHEMA_ROOT / "operator-action-v1.json").read_text(encoding="utf-8")
    )
    assert schema["properties"]["schema_version"]["const"] == "operator-action-v1"
    assert schema["properties"]["action_id"]["pattern"].startswith("^oa_")
    assert schema["properties"]["run_id"]["pattern"].startswith("^fr_")
    assert schema["additionalProperties"] is False
    serialized = json.dumps(schema, sort_keys=True)
    for forbidden in (
        "check_id",
        "limit_name",
        "lifecycle_revision",
        "authority_fingerprint",
        "research_spec_id",
    ):
        assert forbidden not in serialized


def test_v2_controller_contracts_require_public_operator_action_identity() -> None:
    directive = json.loads(
        (SCHEMA_ROOT / "workflow-directive-v2.json").read_text(encoding="utf-8")
    )
    result = json.loads(
        (SCHEMA_ROOT / "research-result-v2.json").read_text(encoding="utf-8")
    )
    assert directive["properties"]["schema_version"]["const"] == (
        "workflow-directive-v2"
    )
    assert result["properties"]["schema_version"]["const"] == "research-result-v2"
    for schema in (directive, result):
        assert schema["properties"]["action_id"]["pattern"].startswith("^oa_")
        assert "action_kind" in schema["required"]
        assert "action_id" in schema["required"]
