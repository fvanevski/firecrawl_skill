"""Issue #311 preview/skeleton contracts after the #314 surface consolidation."""

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any

import pytest

from firecrawl_skill.research_store.research_controller_cli import build_parser

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fsearch_smart"


def _load_script() -> Any:
    loader = SourceFileLoader("issue311_fsearch_smart", str(SCRIPT))
    spec = importlib.util.spec_from_loader("issue311_fsearch_smart", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_legacy_name_contains_no_preview_or_spec_authoring_language() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for retired in (
        "spec_skeleton",
        "dry_run",
        "--spec-skeleton",
        "--dry-run",
        "--research-run-id",
    ):
        assert retired not in source


def test_legacy_name_execs_the_canonical_fresearch_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_script()
    observed: dict[str, Any] = {}

    def fake_execv(path: str, argv: list[str]) -> None:
        observed["path"] = path
        observed["argv"] = list(argv)
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(script.os, "execv", fake_execv)

    with pytest.raises(RuntimeError, match="exec intercepted"):
        script.main(["--retained-only", "bounded objective"])

    target = Path(observed["path"])
    assert target.name == "fresearch"
    assert observed["argv"] == [
        str(target),
        "run",
        "--retained-only",
        "bounded objective",
    ]


def test_current_controller_parser_rejects_retired_preview_flags() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--dry-run", "objective"])
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--spec-skeleton", "objective"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["run", "--research-run-id", "fr_" + "a" * 32, "objective"]
        )
