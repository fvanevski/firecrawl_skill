"""Issue #311 preview/skeleton surface contracts."""

from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any

import pytest

from firecrawl_skill.research_domain import serialize_model
from firecrawl_skill.research_store.smart_search_application import evaluate_budget

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fsearch_smart"


def _load_script() -> Any:
    loader = SourceFileLoader("issue311_fsearch_smart", str(SCRIPT))
    spec = importlib.util.spec_from_loader("issue311_fsearch_smart", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_spec_skeleton_does_not_parse_temporal_language() -> None:
    script = _load_script()
    spec = script.spec_skeleton("latest changes from the past 5 days")
    payload = serialize_model(spec)

    assert payload["time_window"]["start"] is None
    assert payload["time_window"]["end"] is None
    assert payload["freshness_requirements"] == []
    assert "template only" in payload["time_window"]["description"]


def test_limited_preview_uses_explicit_spec_semantics_only() -> None:
    script = _load_script()
    spec = script.spec_skeleton("neutral explicit-spec preview")
    payload = script.dry_run(
        "neutral explicit-spec preview",
        "fc_issue311_preview",
        spec,
        evaluate_budget(spec, 0),
    )

    assert payload["mode"] == "dry_run"
    assert payload["preview_semantics"] == "deterministic_debug_non_predictive"
    assert payload["predictive"] is False
    assert payload["search_plan"]["schema_version"] == "search-plan-v1"
    assert "limitation" in payload["strategy"]


def test_cli_dry_run_raw_objective_is_explicitly_non_predictive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = _load_script()

    assert script.main(["latest changes from the past 5 days", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert '"mode": "dry_run"' in output
    assert '"preview_semantics": "deterministic_debug_non_predictive"' in output
    assert '"predictive": false' in output
    assert '"start": null' in output
    assert '"freshness_requirements": []' in output
