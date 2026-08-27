from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load_smart():
    loader = SourceFileLoader("issue305_fsearch_smart", str(SCRIPTS / "fsearch_smart"))
    spec = importlib.util.spec_from_loader("issue305_fsearch_smart", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_historical_smart_entrypoint_is_policy_free_fresearch_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smart = _load_smart()
    calls: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        calls.append(list(argv))
        return 75

    monkeypatch.setattr(smart, "fresearch_main", fake_main)
    assert smart.main(["same objective", "--retained-only"]) == 75
    assert calls == [["run", "same objective", "--retained-only"]]

    source = (SCRIPTS / "fsearch_smart").read_text(encoding="utf-8")
    for forbidden in (
        "subprocess",
        "SmartObjectiveInterpreter",
        "prepare_run",
        "candidate-budget checks",
        "seal-acquisition",
        "load_planning_bundle",
    ):
        assert forbidden not in source
