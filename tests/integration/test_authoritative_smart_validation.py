from __future__ import annotations

import importlib.util
import json
import os
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""


def load_script(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def smart_module():
    return load_script("rc7_fsearch_smart", SCRIPTS / "fsearch_smart")


def validation_module():
    return load_script("rc7_live_validate", SCRIPTS / "live_validate.py")


def _budget():
    return {
        "policy_version": "budget-policy-v1",
        "policy_config_sha256": "a" * 64,
        "spec_revision": 1,
        "run_revision": 0,
        "selected_tier": "standard",
        "effective_caps": {
            "max_search_branches": 3,
            "max_adaptive_cycles": 2,
        },
    }


def _result(*, outcome="completed", state="completed"):
    return SimpleNamespace(
        outcome=outcome,
        final_state=state,
        wave_count=1,
        successful_urls=1,
        error=None,
        attempted_urls=1,
        successful_attempts=1,
        unsuccessful_urls=0,
        failure_counts={},
        unsuccessful_attempts=(),
    )


def test_smart_dry_run_is_stdout_only_and_has_no_external_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    smart = smart_module()
    monitored = tmp_path / "tmp"
    monitored.mkdir()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry-run performed an external call")

    monkeypatch.setenv("TMPDIR", str(monitored))
    monkeypatch.setattr(smart, "resolved_research_environment", forbidden)
    monkeypatch.setattr(smart.subprocess, "run", forbidden)

    assert smart.main(["bounded dry-run", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "authoritative-smart-search-plan-v1"
    assert payload["mode"] == "dry_run"
    assert payload["queries"][0]["query"] == "bounded dry-run"
    assert list(monitored.rglob("*")) == []


def test_canonical_plan_is_domain_valid_and_targets_the_spec_question():
    from firecrawl_skill.research_domain import load_model
    from firecrawl_skill.research_domain.models import SearchPlan
    from firecrawl_skill.research_store.budget_policy import conservative_research_spec
    from firecrawl_skill.research_store.smart_search_application import canonical_plan

    spec = conservative_research_spec("canonical planning", "general")
    payload = canonical_plan(
        spec,
        [
            {
                "query": "canonical planning primary sources",
                "facet": "primary",
                "intended_source_class": "primary",
                "expected_organizations": ["Example Standards Body"],
                "expected_contribution": "primary evidence",
            }
        ],
    )
    plan = load_model(payload)
    assert isinstance(plan, SearchPlan)
    assert plan.research_spec_id == spec.research_spec_id
    assert plan.queries[0].target_question_ids == (spec.questions[0].question_id,)


def test_failed_authoritative_preflight_prevents_planning_and_execution(
    monkeypatch: pytest.MonkeyPatch,
):
    smart = smart_module()
    planner = mock.Mock(
        side_effect=AssertionError("planning bundle must not initialize")
    )
    executor = mock.Mock(side_effect=AssertionError("orchestrator must not run"))
    monkeypatch.setattr(
        smart,
        "resolved_research_environment",
        lambda: {"DATABASE_URL": "", "PATH": os.environ.get("PATH", "")},
    )
    monkeypatch.setattr(smart, "initialize_planning_bundle", planner)
    monkeypatch.setattr(smart, "execute", executor)

    with pytest.raises(SystemExit) as exc:
        smart.main(["preflight failure", "--research-run-id", "fr_" + "2" * 32])
    assert exc.value.code == 2
    planner.assert_not_called()
    executor.assert_not_called()


def _stub_config_class():
    class _StubConfig:
        @classmethod
        def from_env(cls):
            return cls()

        def require_database(self):
            return None

    return _StubConfig


def test_new_run_planning_proceeds_when_acquisition_preflight_would_fail(
    monkeypatch: pytest.MonkeyPatch,
):
    from firecrawl_skill.research_store import composition, smart_orchestrator
    from firecrawl_skill.research_store import config as config_module
    from firecrawl_skill.research_store.acquisition import authority
    from firecrawl_skill.research_store.budget_policy import (
        conservative_research_spec,
    )
    from firecrawl_skill.research_store.smart_search_application import canonical_plan

    smart = smart_module()
    spec = conservative_research_spec(
        "planning precedes acquisition preflight", "general"
    )
    bundle = SimpleNamespace(
        spec=spec,
        budget=_budget(),
        plan=canonical_plan(spec, [{"query": spec.objective, "facet": "objective"}]),
        spec_row_id="00000000-0000-0000-0000-0000000101",
        spec_revision=1,
        plan_row_id="00000000-0000-0000-0000-0000000201",
        plan_revision=1,
    )
    status = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000002",
        state="created",
        lifecycle_revision=0,
        execution_mode="autonomous_local",
    )
    preflight = mock.Mock(
        side_effect=AssertionError("planning must not wait for acquisition preflight")
    )
    planner = mock.Mock(return_value=bundle)
    executed = mock.Mock(return_value=_result())

    monkeypatch.setattr(
        smart,
        "resolved_research_environment",
        lambda: {
            "DATABASE_URL": "postgresql://test",
            "FIRECRAWL_RESEARCH_AUTO_ENV": "0",
        },
    )
    monkeypatch.setattr(
        smart.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(authority, "require_authoritative_acquisition", preflight)
    monkeypatch.setattr(
        composition,
        "build_run_service",
        lambda _config: SimpleNamespace(status=lambda **_kwargs: status),
    )
    monkeypatch.setattr(config_module, "StoreConfig", _stub_config_class())
    monkeypatch.setattr(smart_orchestrator, "load_planning_bundle", lambda *_args: None)
    monkeypatch.setattr(smart, "initialize_planning_bundle", planner)
    monkeypatch.setattr(smart, "execute", executed)

    assert smart.main([spec.objective, "--research-run-id", "fr_" + "2" * 32]) == 0
    preflight.assert_not_called()
    planner.assert_called_once()
    executed.assert_called_once()


def test_acquiring_run_rerun_reruns_acquisition_preflight_before_network(
    monkeypatch: pytest.MonkeyPatch,
):
    from firecrawl_skill.research_store import composition, smart_orchestrator
    from firecrawl_skill.research_store import config as config_module
    from firecrawl_skill.research_store.acquisition import authority
    from firecrawl_skill.research_store.budget_policy import (
        conservative_research_spec,
    )
    from firecrawl_skill.research_store.smart_search_application import canonical_plan

    smart = smart_module()
    spec = conservative_research_spec(
        "rerun validates acquisition authority", "general"
    )
    bundle = SimpleNamespace(
        spec=spec,
        budget=_budget(),
        plan=canonical_plan(spec, [{"query": spec.objective, "facet": "objective"}]),
        spec_row_id="00000000-0000-0000-0000-0000000101",
        spec_revision=1,
        plan_row_id="00000000-0000-0000-0000-0000000201",
        plan_revision=1,
    )
    status = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000003",
        state="acquiring",
        lifecycle_revision=7,
        execution_mode="autonomous_local",
    )
    preflight = mock.Mock()
    executed = mock.Mock(return_value=_result())

    monkeypatch.setattr(
        smart,
        "resolved_research_environment",
        lambda: {
            "DATABASE_URL": "postgresql://test",
            "FIRECRAWL_RESEARCH_AUTO_ENV": "0",
        },
    )
    monkeypatch.setattr(
        smart.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(authority, "require_authoritative_acquisition", preflight)
    monkeypatch.setattr(
        composition,
        "build_run_service",
        lambda _config: SimpleNamespace(status=lambda **_kwargs: status),
    )
    monkeypatch.setattr(config_module, "StoreConfig", _stub_config_class())
    monkeypatch.setattr(
        smart_orchestrator, "load_planning_bundle", lambda *_args: bundle
    )
    replan = mock.Mock(side_effect=AssertionError("persisted run was replanned"))
    monkeypatch.setattr(smart, "initialize_planning_bundle", replan)
    monkeypatch.setattr(smart, "execute", executed)

    assert smart.main([spec.objective, "--research-run-id", "fr_" + "3" * 32]) == 0
    preflight.assert_called_once()
    replan.assert_not_called()
    executed.assert_called_once()


def test_existing_run_reuses_persisted_bundle_without_replanning(
    monkeypatch: pytest.MonkeyPatch,
):
    from firecrawl_skill.research_store import smart_orchestrator
    from firecrawl_skill.research_store.budget_policy import conservative_research_spec
    from firecrawl_skill.research_store.smart_search_application import canonical_plan

    smart = smart_module()
    spec = conservative_research_spec("resume authoritative run", "general")
    plan = canonical_plan(spec, [{"query": spec.objective, "facet": "objective"}])
    bundle = SimpleNamespace(
        spec=spec,
        budget=_budget(),
        plan=plan,
        spec_row_id="00000000-0000-0000-0000-000000000101",
        spec_revision=1,
        plan_row_id="00000000-0000-0000-0000-000000000201",
        plan_revision=1,
    )
    status = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        state="acquiring",
        lifecycle_revision=4,
        execution_mode="autonomous_local",
    )
    monkeypatch.setattr(
        smart,
        "resolved_research_environment",
        lambda: {
            "DATABASE_URL": "postgresql://test",
            "FIRECRAWL_RESEARCH_AUTO_ENV": "0",
        },
    )
    monkeypatch.setattr(
        smart,
        "prepare_run",
        lambda *_args: ("fr_" + "3" * 32, object(), object(), status),
    )
    monkeypatch.setattr(
        smart_orchestrator, "load_planning_bundle", lambda *_args: bundle
    )
    initializer = mock.Mock(side_effect=AssertionError("persisted run was replanned"))
    monkeypatch.setattr(smart, "initialize_planning_bundle", initializer)
    executed = mock.Mock(return_value=_result())
    monkeypatch.setattr(smart, "execute", executed)

    assert smart.main([spec.objective, "--research-run-id", "fr_" + "3" * 32]) == 0
    initializer.assert_not_called()
    executed.assert_called_once()


def test_terminal_rerun_uses_persisted_outcome_without_planner(
    monkeypatch: pytest.MonkeyPatch,
):
    from firecrawl_skill.research_store import smart_orchestrator
    from firecrawl_skill.research_store.budget_policy import conservative_research_spec
    from firecrawl_skill.research_store.smart_search_application import canonical_plan

    smart = smart_module()
    spec = conservative_research_spec("terminal run", "general")
    bundle = SimpleNamespace(
        spec=spec,
        budget=_budget(),
        plan=canonical_plan(spec, [{"query": spec.objective, "facet": "objective"}]),
        spec_row_id="00000000-0000-0000-0000-000000000101",
        spec_revision=1,
        plan_row_id="00000000-0000-0000-0000-000000000201",
        plan_revision=1,
    )
    status = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        state="completed",
        lifecycle_revision=9,
        execution_mode="autonomous_local",
    )
    monkeypatch.setattr(
        smart,
        "resolved_research_environment",
        lambda: {
            "DATABASE_URL": "postgresql://test",
            "FIRECRAWL_RESEARCH_AUTO_ENV": "0",
        },
    )
    monkeypatch.setattr(
        smart,
        "prepare_run",
        lambda *_args: ("fr_" + "4" * 32, object(), object(), status),
    )
    monkeypatch.setattr(
        smart_orchestrator, "load_planning_bundle", lambda *_args: bundle
    )
    initializer = mock.Mock(side_effect=AssertionError("terminal run was replanned"))
    monkeypatch.setattr(smart, "initialize_planning_bundle", initializer)
    monkeypatch.setattr(smart, "execute", lambda *_args: _result(outcome="resumed"))

    assert smart.main([spec.objective, "--research-run-id", "fr_" + "4" * 32]) == 0
    initializer.assert_not_called()


class _FakeInspector:
    def table_counts(self):
        return {"research_runs": 1, "search_responses": 2}

    def probe_qdrant_alias(self):
        return {
            "alias": "research_chunks_active",
            "collection": "research_chunks_test",
            "dimension": 1024,
            "compatible": True,
        }

    def wait_for_worker(self, external_run_id, _benchmark, **_kwargs):
        return {
            "external_run_id": external_run_id,
            "search_response_count": 1,
            "candidate_count": 1,
            "snapshot_count": 1,
            "document_count": 1,
            "chunk_count": 1,
            "blob_integrity": {"expected": 1, "verified": 1},
            "projection": {"coverage": 1.0, "compatible": True},
            "checks": {"authoritative_records": True},
            "pass": True,
        }


def _validation_args(*, artifact_root=None):
    return SimpleNamespace(
        run_id="test-campaign",
        database_url="postgresql://research@test/research",
        qdrant_url="http://qdrant.test:6333",
        qdrant_api_key="",
        blob_root="/tmp/test-authoritative-blobs",
        max_operations=10,
        api_url="http://firecrawl.test:3002",
        max_adaptive_cycles=1,
        case_timeout=30,
        worker_timeout=0.0,
        artifact_root=artifact_root,
        profile="focused",
    )


def test_live_validation_rejects_persistent_tmpdir_entries(tmp_path: Path):
    validation = validation_module()

    def runner(_command, **kwargs):
        temporary = Path(kwargs["env"]["TMPDIR"])
        (temporary / "unexpected.json").write_text("{}", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    campaign = validation.Campaign(
        _validation_args(),
        inspector=_FakeInspector(),
        runner=runner,
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    try:
        case = campaign.run("residue", ["/bin/true"], json_output=True)
        assert case["status"] == "fail"
        assert case["details"]["temporary_entries"] == ["unexpected.json"]
        assert list(campaign.monitored_tmp.rglob("*")) == []
    finally:
        campaign.close()


def test_live_validation_writes_final_artifacts_only_when_requested(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    validation = validation_module()
    artifact_root = tmp_path / "artifacts"
    campaign = validation.Campaign(
        _validation_args(artifact_root=str(artifact_root)),
        inspector=_FakeInspector(),
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    campaign.cases.append(
        {
            "name": "case",
            "status": "pass",
            "required": True,
            "returncode": 0,
            "seconds": 0.0,
            "operations_after": 0,
            "stdout": "",
            "stderr": "",
            "details": {},
        }
    )
    campaign.runs["run"] = {
        "external_run_id": "fr_" + "5" * 32,
        "benchmark_key": None,
        "require_corpus": True,
    }
    try:
        assert campaign.finish() == 0
        destination = artifact_root / "test-campaign"
        manifest = json.loads((destination / "manifest.json").read_text())
        assert manifest["quality_metrics"][0]["chunk_count"] == 1
        assert "Authoritative run metrics" in (destination / "report.md").read_text()
        assert "Artifacts:" in capsys.readouterr().out
    finally:
        campaign.close()


@pytest.mark.skipif(not TEST_DSN, reason="requires disposable PostgreSQL DSN")
def test_smart_dry_run_does_not_mutate_disposable_postgresql(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import psycopg

    smart = smart_module()
    monitored = tmp_path / "tmp"
    monitored.mkdir()
    monkeypatch.setenv("TMPDIR", str(monitored))

    def counts():
        with psycopg.connect(TEST_DSN) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT
                     (SELECT count(*) FROM research_runs),
                     (SELECT count(*) FROM research_specs),
                     (SELECT count(*) FROM research_budget_snapshots),
                     (SELECT count(*) FROM search_plans),
                     (SELECT count(*) FROM semantic_calls)"""
            )
            return cursor.fetchone()

    before = counts()
    assert smart.main(["database purity", "--dry-run"]) == 0
    assert counts() == before
    assert list(monitored.rglob("*")) == []


def test_rc7_runtime_sources_have_no_removed_storage_markers():
    markers = (
        "firecrawl_" + "scratch",
        "SCRATCH_" + "ROOT",
        "scratch_" + "file",
        "persist_" + "results.py",
        "import-" + "scratch",
        "_corpus" + ".json",
    )
    for path in (SCRIPTS / "fsearch_smart", SCRIPTS / "live_validate.py"):
        text = path.read_text(encoding="utf-8")
        assert not any(marker in text for marker in markers)
