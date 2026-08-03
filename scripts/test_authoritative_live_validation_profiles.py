from __future__ import annotations

import importlib.util
import json
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parent


def validation_module():
    loader = SourceFileLoader(
        "rc7_live_validation_profiles",
        str(SCRIPTS / "live_validate.py"),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("profile", "cap"),
    (("focused", 40), ("failure-path", 20), ("full", 100)),
)
def test_validation_profiles_enforce_hard_operation_caps(profile: str, cap: int):
    validation = validation_module()

    args = validation.parse_args(["--profile", profile, "--max-operations", str(cap)])
    assert args.profile == profile
    assert args.max_operations == cap

    with pytest.raises(SystemExit) as exc:
        validation.parse_args(["--profile", profile, "--max-operations", str(cap + 1)])
    assert exc.value.code == 2


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

    args = SimpleNamespace(
        run_id="cap-test",
        database_url="postgresql://research@test/research",
        qdrant_url="http://qdrant.test:6333",
        qdrant_api_key="",
        blob_root=str(tmp_path / "blobs"),
        max_operations=1,
        api_url="http://firecrawl.test:3002",
        max_adaptive_cycles=1,
        case_timeout=30,
        worker_timeout=0.0,
        artifact_root=None,
        profile="focused",
    )
    campaign = validation.Campaign(
        args,
        inspector=mock.Mock(),
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
            "operation cap exhausted" in stderr.lower() for _stdout, stderr in results
        )
    finally:
        campaign.close()


def test_failure_path_dispatch_excludes_success_campaigns():
    validation = validation_module()
    campaign = object.__new__(validation.Campaign)
    campaign.args = SimpleNamespace(profile="failure-path")
    campaign.preflight = mock.Mock(return_value=True)
    campaign.validate_dry_run = mock.Mock()
    campaign.run_fscrape_valkey_loss = mock.Mock()
    campaign.run_smart = mock.Mock()
    campaign.run_full_cases = mock.Mock()
    campaign.finish = mock.Mock(return_value=0)

    assert validation.Campaign.execute(campaign) == 0

    campaign.preflight.assert_called_once_with()
    campaign.validate_dry_run.assert_called_once_with()
    campaign.run_fscrape_valkey_loss.assert_called_once_with()
    campaign.run_smart.assert_not_called()
    campaign.run_full_cases.assert_not_called()
    campaign.finish.assert_called_once_with()
