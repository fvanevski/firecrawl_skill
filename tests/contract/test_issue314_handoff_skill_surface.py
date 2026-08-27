from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from firecrawl_skill.research_store.orchestrator import SynthesisStage
from firecrawl_skill.research_store.research_controller_cli import build_parser
from firecrawl_skill.research_store.research_controller_contract import (
    DELIVERY_HOST_HANDOFF,
    RESULT_SCHEMA_VERSION,
)

ROOT = Path(__file__).resolve().parents[2]


def test_one_normal_smart_entrypoint_and_public_controller_parser() -> None:
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "scripts/fresearch run" in skill
    assert "scripts/fsearch_smart\" \"<topic>" not in skill
    for forbidden in (
        "scripts/frun prepare",
        "scripts/frun seal-acquisition",
        "scripts/frun resume",
        "scripts/frun synthesize",
        "candidate-budget checks",
    ):
        assert forbidden not in skill

    parser = build_parser()
    run = parser.parse_args(["run", "objective"])
    assert run.delivery_mode == DELIVERY_HOST_HANDOFF
    assert parser.parse_args(["continue", "fr_" + "a" * 32]).command == "continue"
    assert parser.parse_args(["result", "fr_" + "a" * 32]).command == "result"


def test_result_and_handoff_schemas_are_current() -> None:
    result = json.loads(
        (ROOT / "schemas/research-workflow/research-result-v3.json").read_text(
            encoding="utf-8"
        )
    )
    handoff = json.loads(
        (ROOT / "schemas/research-workflow/research-handoff-v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert RESULT_SCHEMA_VERSION == "research-result-v3"
    assert result["properties"]["run_id"]["pattern"].startswith("^fr_")
    assert {"delivery_mode", "handoff"} <= set(result["required"])
    assert handoff["properties"]["run_id"]["pattern"].startswith("^fr_")
    assert "citation_ready" in handoff["required"]


def test_host_handoff_skips_full_prose_and_preserves_validation_transition() -> None:
    transitions: list[dict[str, object]] = []

    class Runs:
        evidence_service = object()

        def transition(self, run_id, next_state, **kwargs):
            transitions.append({"run_id": run_id, "next_state": next_state, **kwargs})

    stage = SynthesisStage(Runs(), SimpleNamespace(), evidence_service=object())
    run_id = uuid4()
    result = stage.execute(
        run_id,
        7,
        4,
        "synthesizing",
        {"evidence_packet_revision": 3, "delivery_mode": "host_handoff"},
    )
    assert result.error is None
    assert transitions[0]["next_state"] == "validating"
    assert "skipped redundant full-prose synthesis" in result.summary


def test_canonical_docs_use_public_actions_and_scope_fork() -> None:
    workflow = (ROOT / "references/authoritative-workflows.md").read_text(
        encoding="utf-8"
    )
    assert "scripts/fresearch action oa_<uuid>" in workflow
    assert "scripts/fresearch fork oa_<uuid>" in workflow
    assert "creates a child run" in workflow
    assert "objective_satisfied=false" in workflow
    assert "scripts/frun prepare" not in workflow
    assert "scripts/frun finish" not in workflow
