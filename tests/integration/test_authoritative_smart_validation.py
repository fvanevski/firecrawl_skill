from __future__ import annotations

import ast
import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
ROOT = SCRIPTS.parent


def load_script(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def validation_module():
    return load_script("authoritative_live_validate", SCRIPTS / "live_validate.py")


def test_deprecated_smart_name_is_only_canonical_wrapper_delegate() -> None:
    source = (SCRIPTS / "fsearch_smart").read_text(encoding="utf-8")
    assert 'with_name("fresearch")' in source
    assert "os.execv" in source
    assert '"run", *args' in source
    for retired_owner in (
        "resolved_research_environment",
        "initialize_planning_bundle",
        "load_planning_bundle",
        "require_authoritative_acquisition",
        "build_production_resumable_orchestrator",
        "--research-spec",
        "--research-run-id",
        "--dry-run",
    ):
        assert retired_owner not in source


def test_canonical_plan_is_domain_valid_and_targets_the_spec_question() -> None:
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


def test_controller_and_acquisition_have_distinct_authority_owners() -> None:
    controller = (
        ROOT
        / "src"
        / "firecrawl_skill"
        / "research_store"
        / "research_controller.py"
    ).read_text(encoding="utf-8")
    acquisition = (
        ROOT
        / "src"
        / "firecrawl_skill"
        / "research_store"
        / "acquisition"
        / "service.py"
    ).read_text(encoding="utf-8")

    assert "initialize_planning_bundle" in controller
    assert "load_planning_bundle" in controller
    assert "build_production_resumable_orchestrator" in controller
    assert "authority_preflight" in acquisition
    assert "require_authoritative_acquisition" not in controller

    acquisition_tree = ast.parse(acquisition)
    service_class = next(
        node
        for node in acquisition_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AcquisitionService"
    )
    resolve_method = next(
        node
        for node in service_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_resolve_authority_context"
    )
    assert any(
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "AcquisitionPreflightError"
        for node in ast.walk(resolve_method)
    )

    execute_method = next(
        node
        for node in service_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "execute_search"
    )
    call_lines = {
        node.func.attr: node.lineno
        for node in ast.walk(execute_method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"_resolve_authority_context", "search"}
    }
    assert call_lines["_resolve_authority_context"] < call_lines["search"]


def test_current_controller_parser_has_no_retired_manual_resume_or_preview_inputs() -> None:
    from firecrawl_skill.research_store.research_controller_cli import build_parser

    parser = build_parser()
    run_id = "fr_" + "a" * 32
    assert parser.parse_args(["continue", run_id]).command == "continue"
    assert parser.parse_args(["result", run_id]).command == "result"
    for argv in (
        ["run", "--dry-run", "objective"],
        ["run", "--research-spec", "spec.json", "objective"],
        ["run", "--research-run-id", run_id, "objective"],
        ["run", "--max-adaptive-cycles", "2", "objective"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


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


def test_live_validation_rejects_persistent_tmpdir_entries(tmp_path: Path) -> None:
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
) -> None:
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


def test_current_runtime_sources_have_no_removed_storage_markers() -> None:
    markers = (
        "firecrawl_" + "scratch",
        "SCRATCH_" + "ROOT",
        "scratch_" + "file",
        "persist_" + "results.py",
        "import-" + "scratch",
        "_corpus" + ".json",
    )
    for path in (
        SCRIPTS / "fresearch",
        SCRIPTS / "fsearch_smart",
        SCRIPTS / "live_validate.py",
    ):
        text = path.read_text(encoding="utf-8")
        assert not any(marker in text for marker in markers)
