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
    loader = SourceFileLoader("issue305_fsearch_smart", str(SCRIPTS / "fsearch_smart"))
    spec = importlib.util.spec_from_loader("issue305_fsearch_smart", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_operator_action_prints_exact_recovery_and_exits_75(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from firecrawl_skill.research_store import smart_orchestrator

    smart = _load_smart()
    external_id = "fr_" + "a" * 32
    run_id = uuid4()
    check_id = uuid4()
    spec = conservative_research_spec("issue 305 recovery", "general")
    bundle = SimpleNamespace(
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
    status = SimpleNamespace(
        id=run_id,
        state="indexing",
        execution_mode="autonomous_local",
    )
    result = SimpleNamespace(
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
        operator_action={
            "kind": "candidate_budget_override_required",
            "run_id": str(run_id),
            "lifecycle_revision": 7,
            "check_id": str(check_id),
            "scope": {"subject_ids": [str(uuid4())]},
            "scope_fingerprint": "a" * 64,
            "violated_limits": ["max_generic_page_share"],
        },
    )

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
        lambda *_args: (external_id, object(), object(), status),
    )
    monkeypatch.setattr(smart_orchestrator, "load_planning_bundle", lambda *_: bundle)
    monkeypatch.setattr(smart, "execute", lambda *_args: result)

    assert smart.main([spec.objective, "--research-run-id", external_id]) == 75
    captured = capsys.readouterr()
    assert f"Run ID: {external_id}" in captured.out
    assert "Orchestrator outcome: operator_action_required" in captured.out
    assert (
        "Next action: resolve_candidate_budget_override_then_resume_same_run"
        in captured.out
    )
    assert f"scripts/candidate-budget checks {external_id}" in captured.out
    assert str(check_id) in captured.out
    assert "max_generic_page_share" in captured.out
    assert f"--research-run-id {external_id}" in captured.out
    assert "outcome=operator_action_required" in captured.err
