"""Regression tests for the centralized Pyrefly/static validation contract."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
CI_REQUIREMENTS = ROOT / "requirements-ci.txt"
BASELINE = ROOT / "pyrefly-baseline.json"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
BASELINE_WORKFLOW = ROOT / ".github" / "workflows" / "pyrefly-baseline.yml"
LOCAL_AGENT_CONTRACT = ROOT / "references" / "local-agent-validation.md"
TRANSITION = ROOT / "ci" / "merge-policy-transition.toml"


def _transition_state() -> str:
    return str(
        tomllib.loads(TRANSITION.read_text(encoding="utf-8"))["transition_state"]
    )


def test_pyrefly_is_centrally_pinned_and_configured() -> None:
    requirements = CI_REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    assert "pytest==9.1.1" in requirements
    assert "ruff==0.16.5" in requirements
    assert "pyrefly==1.2.0" in requirements
    legacy = ROOT / "requirements-typecheck.txt"
    if _transition_state() == "pending-exact-head-proof":
        assert legacy.exists()
    else:
        assert not legacy.exists()

    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["pyrefly"]
    assert config["baseline"] == "pyrefly-baseline.json"
    assert config["python-version"] == "3.12"
    assert config["python-platform"] == "linux"
    assert config["search-path"] == ["scripts", "src", "."]
    assert config["project-includes"] == ["scripts/**/*.py", "src/**/*.py"]
    assert config["project-excludes"] == [
        "scripts/test_*.py",
        "scripts/**/test_*.py",
    ]


def test_checked_in_baseline_is_structured_debt_not_inline_suppression() -> None:
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert isinstance(payload.get("errors"), list)
    assert payload["errors"]
    for error in payload["errors"]:
        assert isinstance(error.get("path"), str)
        assert isinstance(error.get("name"), str)
        assert error.get("severity") == "error"


def test_ci_runs_one_static_profile_and_preserves_transition_contexts() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "\n  pyrefly:\n    name: Pyrefly\n" in workflow
    assert "\n  merge-gate:\n    name: Merge gate\n" in workflow
    assert workflow.count("--profile static") == 1
    assert "scripts/run_ci_profile.py" in workflow
    assert "requirements-ci.txt" in workflow
    assert "requirements-typecheck.txt" not in workflow
    assert "mypy" not in workflow.lower()
    assert "3.11" not in workflow
    assert "--update-baseline" not in workflow

    transition = tomllib.loads(TRANSITION.read_text(encoding="utf-8"))
    assert transition["old_required_check"] == "Pyrefly"
    assert transition["new_required_check"] == "Merge gate"
    assert transition["transition_state"] in {
        "pending-exact-head-proof",
        "retired-awaiting-ruleset-cutover",
        "complete",
    }


def test_baseline_regeneration_is_manual_only_and_uses_central_toolchain() -> None:
    workflow = BASELINE_WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert "push:" not in workflow
    assert "contents: read" in workflow
    assert "requirements-ci.txt" in workflow
    assert "requirements-typecheck.txt" not in workflow
    assert "--update-baseline" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "continue-on-error: true" not in workflow


def test_local_agent_contract_uses_central_profile_vocabulary() -> None:
    contract = LOCAL_AGENT_CONTRACT.read_text(encoding="utf-8")
    for required in (
        "requirements-ci.txt",
        "scripts/ci_plan.py",
        "scripts/run_ci_profile.py",
        "static",
        "core",
        "acquisition",
        "storage",
        "controller",
        "retrieval",
        "migration",
        "Merge gate",
        "scripts/local-agent-assessment",
        "--import-mode=importlib",
    ):
        assert required in contract
    assert "Python **3.12**" in contract
    assert "3.11" not in contract
    assert "Mypy" in contract  # explicit prohibition, not an active authority


def test_operator_scripts_namespace_resolves_without_baseline_configuration_debt() -> None:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["pyrefly"]
    assert config["search-path"] == ["scripts", "src", "."]

    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    scripts_missing_imports = [
        error
        for error in payload["errors"]
        if error.get("name") == "missing-import"
        and _missing_import_module(error).startswith("scripts.")
    ]
    assert scripts_missing_imports == []


def _missing_import_module(error: dict[str, object]) -> str:
    description = error.get("concise_description")
    if not isinstance(description, str):
        return ""
    marker = "Cannot find module `"
    if not description.startswith(marker) or not description.endswith("`"):
        return ""
    return description[len(marker) : -1]
