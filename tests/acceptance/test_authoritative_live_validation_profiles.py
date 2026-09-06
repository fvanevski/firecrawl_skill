from __future__ import annotations

import importlib.util
import json
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def validation_module():
    loader = SourceFileLoader(
        "issue359_live_validation",
        str(SCRIPTS / "live_validate.py"),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("profile", "cap"),
    (("focused", 40), ("failure-path", 20), ("full", 100), ("destructive", 10)),
)
def test_validation_profiles_enforce_hard_operation_caps(profile: str, cap: int):
    validation = validation_module()

    defaulted = validation.parse_args(["--profile", profile])
    assert defaulted.max_operations == cap

    args = validation.parse_args(
        ["--profile", profile, "--max-operations", str(cap)]
    )
    assert args.profile == profile
    assert args.max_operations == cap

    with pytest.raises(SystemExit) as exc:
        validation.parse_args(
            ["--profile", profile, "--max-operations", str(cap + 1)]
        )
    assert exc.value.code == 2


def test_validator_rejects_retired_smart_options_in_its_own_cli():
    validation = validation_module()

    with pytest.raises(SystemExit) as exc:
        validation.parse_args(["--max-adaptive-cycles", "1"])
    assert exc.value.code == 2


def _args(tmp_path: Path, **overrides):
    values = {
        "run_id": "issue359-test",
        "database_url": "postgresql://research@test/research",
        "qdrant_url": "http://qdrant.test:6333",
        "qdrant_api_key": "",
        "blob_root": str(tmp_path / "blobs"),
        "max_operations": 4,
        "api_url": "http://firecrawl.test:3002",
        "case_timeout": 30,
        "worker_timeout": 0.0,
        "expected_head_sha": None,
        "artifact_root": None,
        "profile": "focused",
        "keep_runs": False,
        "disposable_namespace": "fc_live_fault",
        "disposable_pg_port": 55436,
        "disposable_qdrant_port": 55437,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Inspector:
    def __init__(self):
        self.states: dict[str, str] = {}
        self.run_ids: set[str] = set()
        self.objectives: dict[str, set[str]] = {}

    def list_run_ids(self):
        return set(self.run_ids)

    def run_ids_for_objective(self, objective):
        return set(self.objectives.get(objective, set()))

    def run_state(self, run_id):
        return self.states[run_id]

    def probe_qdrant_alias(self):
        return {
            "alias": "research_chunks_active",
            "collection": "research_chunks_test",
            "dimension": 1024,
            "compatible": True,
        }

    def wait_for_worker(self, run_id, **_kwargs):
        return {
            "external_run_id": run_id,
            "checks": {"authoritative_records": True},
            "pass": True,
        }


def test_firecrawl_proxy_enforces_cap_under_concurrent_calls(tmp_path: Path):
    validation = validation_module()
    call_log = tmp_path / "real-firecrawl.log"
    real_cli = tmp_path / "real-firecrawl"
    real_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, time\n"
        "time.sleep(0.15)\n"
        "path = pathlib.Path(os.environ['REAL_CALL_LOG'])\n"
        "with path.open('a', encoding='utf-8') as handle:\n"
        "    handle.write('called\\n')\n",
        encoding="utf-8",
    )
    real_cli.chmod(0o700)

    campaign = validation.Campaign(
        _args(tmp_path, max_operations=1),
        inspector=_Inspector(),
        real_cli=str(real_cli),
        work_root=tmp_path / "work",
    )
    campaign.env["REAL_CALL_LOG"] = str(call_log)
    proxy = campaign.proxy_dir / "firecrawl"
    try:
        processes = [
            subprocess.Popen(
                [str(proxy), "search", f"query-{index}"],
                env=campaign.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for index in range(2)
        ]
        results = [process.communicate(timeout=10) for process in processes]
        returncodes = sorted(process.returncode for process in processes)

        assert returncodes == [0, 78]
        assert call_log.read_text(encoding="utf-8").splitlines() == ["called"]
        counter = json.loads(campaign.counter.read_text(encoding="utf-8"))
        assert counter["count"] == 1
        assert len(counter["calls"]) == 1
        assert any(
            "operation cap exhausted" in stderr.lower()
            for _stdout, stderr in results
        )
    finally:
        campaign.close()


def test_contract_conformance_does_not_imply_capability_success(tmp_path: Path):
    validation = validation_module()
    payload = {
        "schema_version": "research-result-v3",
        "run_id": "fr_" + "1" * 32,
        "lifecycle_state": "partial",
        "disposition": "terminal_partial",
        "terminal": True,
        "objective_satisfied": False,
    }

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    campaign = validation.Campaign(
        _args(tmp_path),
        inspector=_Inspector(),
        runner=runner,
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    try:
        case = campaign.run(
            "typed_partial",
            ["fresearch"],
            json_output=True,
            expected_returncodes=(0, 1, 75),
            required_capability=True,
            capability_evaluator=lambda value, rc: bool(
                rc == 0
                and value
                and value.get("disposition") == "terminal_completed"
                and value.get("objective_satisfied") is True
            ),
        )
        assert validation._fresearch_contract(payload, 0) is True
        assert case["contract_result"] == "PASS"
        assert case["capability_result"] == "FAIL"
        assert case["observed_disposition"] == "terminal_partial"
    finally:
        campaign.close()


def test_retired_smart_matrix_requires_zero_provider_activity(tmp_path: Path):
    validation = validation_module()
    inspector = _Inspector()

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="rejected")

    campaign = validation.Campaign(
        _args(tmp_path),
        inspector=inspector,
        runner=runner,
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    try:
        campaign.validate_retired_smart_options()
        retired = [
            case for case in campaign.cases
            if case["name"].startswith("retired_smart_option_")
        ]
        assert len(retired) == 4
        assert all(case["contract_result"] == "PASS" for case in retired)
        assert all(
            case["details"]["provider_operation_delta"] == 0 for case in retired
        )
        assert all(case["capability_result"] == "NOT_EVALUATED" for case in retired)
    finally:
        campaign.close()

# __GHDEV_APPEND__
