"""Issue #307: operator-facing temporal coverage gap print path in fsearch_smart."""

from __future__ import annotations

import importlib.util
import os
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.budget_policy import conservative_research_spec
from firecrawl_skill.research_store.smart_search_application import canonical_plan

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load_smart():
    loader = SourceFileLoader(
        "issue307_temporal_cli_fsearch_smart", str(SCRIPTS / "fsearch_smart")
    )
    spec = importlib.util.spec_from_loader(
        "issue307_temporal_cli_fsearch_smart", loader
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _make_result(operator_action: dict) -> SimpleNamespace:
    run_id = uuid4()
    return SimpleNamespace(
        run_id=run_id,
        outcome="operator_action_required",
        final_state="indexing",
        wave_count=1,
        successful_urls=1,
        attempted_urls=1,
        successful_attempts=1,
        unsuccessful_urls=0,
        failure_counts={},
        unsuccessful_attempts=(),
        error=None,
        operator_action=operator_action,
    )


def _bundle(topic: str):
    spec = conservative_research_spec(topic, "general")
    return SimpleNamespace(
        spec=spec,
        budget={
            "policy_version": "budget-policy-v1",
            "effective_caps": {"max_adaptive_cycles": 1},
        },
        plan=canonical_plan(spec, [{"query": spec.objective, "facet": "objective"}]),
        spec_row_id=uuid4(),
        spec_revision=1,
        plan_row_id=uuid4(),
        plan_revision=1,
    )


def _patch_seams(
    monkeypatch: pytest.MonkeyPatch, smart, bundle, external_id: str, result
) -> None:
    from firecrawl_skill.research_store import smart_orchestrator

    monkeypatch.setattr(
        smart,
        "resolved_research_environment",
        lambda: {
            "DATABASE_URL": "postgresql://test",
            "FIRECRAWL_RESEARCH_AUTO_ENV": "0",
            "PATH": os.environ.get("PATH", ""),
        },
    )
    monkeypatch.setattr(
        smart,
        "prepare_run",
        lambda *_args: (
            external_id,
            object(),
            object(),
            SimpleNamespace(id=result.run_id, state="indexing"),
        ),
    )
    monkeypatch.setattr(smart_orchestrator, "load_planning_bundle", lambda *_: bundle)
    monkeypatch.setattr(smart, "execute", lambda *_args: result)


def test_temporal_coverage_gap_prints_bounded_summary_and_exits_75(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    smart = _load_smart()
    external_id = "fr_" + "b" * 32
    check_id = uuid4()
    result = _make_result(
        {
            "kind": "temporal_coverage_gap",
            "run_id": str(uuid4()),
            "lifecycle_revision": 4,
            "check_id": str(check_id),
            "diagnostics": {
                "basis": "publication_window",
                "qualifying_passages": 0,
                "examined_passages": 1,
                "missing_publication_authority": 1,
                "publication_out_of_window": 0,
                "missing_freshness_authority": 0,
                "stale_freshness_authority": 0,
            },
        }
    )
    bundle = _bundle("issue 307 temporal gap")
    _patch_seams(monkeypatch, smart, bundle, external_id, result)

    assert smart.main([bundle.spec.objective, "--research-run-id", external_id]) == 75
    captured = capsys.readouterr()
    assert f"Run ID: {external_id}" in captured.out
    assert "Orchestrator outcome: operator_action_required" in captured.out
    assert (
        "Next action: resolve_temporal_coverage_gap_then_resume_same_run"
        in captured.out
    )
    assert (
        "Temporal coverage: unsatisfied; basis=publication_window; "
        "qualifying=0/1; missing_publication=1; publication_out_of_window=0; "
        "missing_freshness=0; stale_freshness=0" in captured.out
    )
    # Candidate-budget recovery is a separate disposition: nothing of it leaks
    # into the temporal print path.
    assert "scripts/candidate-budget" not in captured.out
    assert "outcome=operator_action_required" in captured.err


def test_candidate_budget_action_does_not_print_temporal_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    smart = _load_smart()
    external_id = "fr_" + "c" * 32
    check_id = uuid4()
    result = _make_result(
        {
            "kind": "candidate_budget_override_required",
            "run_id": str(uuid4()),
            "lifecycle_revision": 7,
            "check_id": str(check_id),
            "scope": {"subject_ids": [str(uuid4())]},
            "scope_fingerprint": "a" * 64,
            "violated_limits": ["max_generic_page_share"],
        }
    )
    bundle = _bundle("issue 307 budget gap")
    _patch_seams(monkeypatch, smart, bundle, external_id, result)

    assert smart.main([bundle.spec.objective, "--research-run-id", external_id]) == 75
    captured = capsys.readouterr()
    assert "Next action: resolve_candidate_budget_override_then_resume_same_run" in (
        captured.out
    )
    assert f"scripts/candidate-budget checks {external_id}" in captured.out
    assert "Temporal coverage:" not in captured.out
