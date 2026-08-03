from __future__ import annotations

import importlib.util
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
