"""Issue #305 recovery contracts after durable operator actions superseded CLI recipes."""

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any

import pytest

from firecrawl_skill.research_store.research_controller_cli import build_parser

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load_smart() -> Any:
    path = SCRIPTS / "fsearch_smart"
    loader = SourceFileLoader("issue305_fsearch_smart", str(path))
    spec = importlib.util.spec_from_loader("issue305_fsearch_smart", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_legacy_smart_name_delegates_without_generated_recovery_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart = _load_smart()
    observed: dict[str, Any] = {}

    def fake_execv(path: str, argv: list[str]) -> None:
        observed["path"] = path
        observed["argv"] = list(argv)
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(smart.os, "execv", fake_execv)
    with pytest.raises(RuntimeError, match="exec intercepted"):
        smart.main(["research objective"])

    target = Path(observed["path"])
    assert target.name == "fresearch"
    assert observed["argv"] == [str(target), "run", "research objective"]

    source = (SCRIPTS / "fsearch_smart").read_text(encoding="utf-8")
    for retired in (
        "candidate-budget",
        "check_id",
        "scope_fingerprint",
        "violated_limits",
        "Next action",
        "--research-run-id",
    ):
        assert retired not in source


def test_soft_budget_authorization_public_cli_needs_only_action_and_human_decision() -> (
    None
):
    parser = build_parser()
    action_id = "oa_" + "a" * 32
    parsed = parser.parse_args(
        [
            "approve",
            action_id,
            "--reason",
            "bounded exception approved",
            "--authorized-by",
            "human",
        ]
    )
    assert parsed.command == "approve"
    assert parsed.action_id == action_id
    assert "check_id" not in vars(parsed)
    assert "scope_fingerprint" not in vars(parsed)
    assert "violated_limits" not in vars(parsed)
