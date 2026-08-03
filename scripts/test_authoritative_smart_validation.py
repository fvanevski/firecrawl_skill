from __future__ import annotations

import importlib.util
import json
import os
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parent
TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")


def load_script(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def smart_module():
    return load_script("rc7_fsearch_smart", SCRIPTS / "fsearch_smart")


def validation_module():
    return load_script("rc7_live_validate", SCRIPTS / "live_validate.py")


def _successful_result():
    return SimpleNamespace(
        outcome="satisfied",
        final_state="completed",
        wave_count=1,
        successful_urls=("https://example.com",),
        error=None,
    )


def test_smart_dry_run_is_stdout_only_and_has_no_external_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    smart = smart_module()
    monitored = tmp_path / "tmp"
    monitored.mkdir()
    monkeypatch.setenv("TMPDIR", str(monitored))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry-run performed an external call")

    monkeypatch.setattr(smart, "resolved_research_environment", forbidden)
    monkeypatch.setattr(smart.subprocess, "run", forbidden)

    result = smart.main(
        [
            "bounded authoritative dry-run",
            "--dry-run",
            "--invocation-id",
            "fc_" + "1" * 32,
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "authoritative-smart-search-plan-v1"
    assert payload["mode"] == "dry_run"
    assert payload["queries"][0]["query"] == "bounded authoritative dry-run"
    assert list(monitored.rglob("*")) == []


def test_smart_failed_authoritative_preflight_prevents_planning_and_network(
    monkeypatch: pytest.MonkeyPatch,
):
    smart = smart_module()
    planner = mock.Mock(side_effect=AssertionError("planner must not run"))
    executor = mock.Mock(side_effect=AssertionError("orchestrator must not run"))
    monkeypatch.setattr(
        smart,
        "resolved_research_environment",
        lambda: {"DATABASE_URL": "", "PATH": os.environ.get("PATH", "")},
    )
    monkeypatch.setattr(smart, "generate_search_plan", planner)
    monkeypatch.setattr(smart, "execute_orchestrator", executor)

    with pytest.raises(SystemExit) as exc:
        smart.main(
            [
                "preflight failure",
                "--research-run-id",
                "fr_" + "2" * 32,
            ]
        )

    assert exc.value.code == 2
    planner.assert_not_called()
    executor.assert_not_called()


def test_smart_normal_and_resume_use_only_authoritative_run_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    smart = smart_module()
    monitored = tmp_path / "tmp"
    monitored.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": "postgresql://research@test/research",
            "TMPDIR": str(monitored),
            "FIRECRAWL_RESEARCH_AUTO_ENV": "0",
        }
    )
    run_id = "fr_" + "3" * 32
    status = SimpleNamespace(state="acquiring")
    prepared: list[str] = []
    executed: list[str] = []

    def prepare(value, _topic, _environment):
        prepared.append(value)
        return value, object(), status

    def execute(value, *_args, **_kwargs):
        executed.append(value)
        return _successful_result()

    monkeypatch.setattr(smart, "resolved_research_environment", lambda: environment)
    monkeypatch.setattr(smart, "prepare_authoritative_run", prepare)
    monkeypatch.setattr(
        smart,
        "generate_search_plan",
        lambda topic, max_queries: (
            [
                {
                    "query": topic,
                    "facet": "objective",
                    "subquestion": topic,
                }
            ],
            {"status": "generated"},
            {"status": "not_run"},
        ),
    )
    monkeypatch.setattr(smart, "execute_orchestrator", execute)

    for _ in range(2):
        assert (
            smart.main(
                [
                    "resume authoritative run",
                    "--research-run-id",
                    run_id,
                    "--max-adaptive-cycles",
                    "1",
                ]
            )
            == 0
        )

    assert prepared == [run_id, run_id]
    assert executed == [run_id, run_id]
    assert list(monitored.rglob("*")) == []
    assert not hasattr(smart, "write_scratch_diagnostics")


class _FakeInspector:
    def table_counts(self):
        return {
            "research_runs": 1,
            "research_invocations": 2,
            "search_responses": 3,
            "search_candidates": 4,
            "extraction_attempts": 5,
            "asset_snapshots": 6,
            "documents": 7,
            "chunks": 8,
            "research_events": 9,
            "index_jobs": 10,
        }

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
            "candidate_count": 2,
            "snapshot_count": 1,
            "document_count": 1,
            "chunk_count": 2,
            "projection": {"coverage": 1.0, "compatible": True},
            "checks": {
                "authoritative_records": True,
                "worker_completed": True,
                "qdrant_coverage": True,
            },
            "pass": True,
        }


def _validation_args(*, artifact_root=None):
    return SimpleNamespace(
        run_id="test-campaign",
        database_url="postgresql://research@test/research",
        qdrant_url="http://qdrant.test:6333",
        qdrant_api_key="",
        max_operations=10,
        api_url="http://firecrawl.test:3002",
        max_adaptive_cycles=1,
        case_timeout=30,
        worker_timeout=0.0,
        artifact_root=artifact_root,
        profile="focused",
    )


def test_live_validation_failed_store_preflight_stops_before_network(tmp_path: Path):
    validation = validation_module()
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="database unavailable",
        )

    campaign = validation.Campaign(
        _validation_args(),
        inspector=_FakeInspector(),
        runner=runner,
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    try:
        assert campaign.preflight() is False
    finally:
        campaign.close()

    assert len(calls) == 1
    assert calls[0][-1] == "ingest-ready"
    assert not any("fsearch" in str(part) for command in calls for part in command)
    assert not any("fscrape" in str(part) for command in calls for part in command)


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
        case = campaign.run(
            "residue",
            ["/bin/true"],
            json_output=True,
        )
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
    no_artifacts = validation.Campaign(
        _validation_args(),
        inspector=_FakeInspector(),
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work-no-artifacts",
    )
    no_artifacts.cases.append(
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
    no_artifacts.runs["run"] = {
        "external_run_id": "fr_" + "4" * 32,
        "benchmark_key": None,
        "require_corpus": True,
    }
    try:
        assert no_artifacts.finish() == 0
        assert not list((tmp_path / "work-no-artifacts").rglob("manifest.json"))
        assert not list((tmp_path / "work-no-artifacts").rglob("report.md"))
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema_version"] == "authoritative-live-validation-v1"
    finally:
        no_artifacts.close()

    artifact_root = tmp_path / "artifacts"
    requested = validation.Campaign(
        _validation_args(artifact_root=str(artifact_root)),
        inspector=_FakeInspector(),
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work-requested",
    )
    requested.cases.append(
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
    requested.runs["run"] = {
        "external_run_id": "fr_" + "5" * 32,
        "benchmark_key": None,
        "require_corpus": True,
    }
    try:
        assert requested.finish() == 0
        destination = artifact_root / "test-campaign"
        manifest = json.loads(
            (destination / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["quality_metrics"][0]["snapshot_count"] == 1
        assert manifest["quality_metrics"][0]["document_count"] == 1
        assert manifest["quality_metrics"][0]["chunk_count"] == 2
        report = (destination / "report.md").read_text(encoding="utf-8")
        assert "Authoritative run metrics" in report
    finally:
        requested.close()


@pytest.mark.skipif(
    not TEST_DSN,
    reason="requires explicit disposable PostgreSQL test DSN",
)
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
                     (SELECT count(*) FROM research_invocations),
                     (SELECT count(*) FROM search_responses),
                     (SELECT count(*) FROM search_candidates),
                     (SELECT count(*) FROM extraction_attempts),
                     (SELECT count(*) FROM asset_snapshots),
                     (SELECT count(*) FROM documents),
                     (SELECT count(*) FROM chunks),
                     (SELECT count(*) FROM research_events),
                     (SELECT count(*) FROM index_jobs)"""
            )
            return cursor.fetchone()

    before = counts()
    assert smart.main(["disposable database purity", "--dry-run"]) == 0
    after = counts()

    assert before == after
    assert list(monitored.rglob("*")) == []


def test_rc7_runtime_sources_have_no_removed_storage_markers():
    markers = (
        "firecrawl_" + "scratch",
        "SCRATCH_" + "ROOT",
        "scratch_" + "file",
        "scratch_" + "dir",
        "_meta" + ".json",
        "_index" + ".md",
        "persist_" + "results.py",
        "import-" + "scratch",
    )
    for path in (SCRIPTS / "fsearch_smart", SCRIPTS / "live_validate.py"):
        text = path.read_text(encoding="utf-8")
        assert not any(marker in text for marker in markers)
