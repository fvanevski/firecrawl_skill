"""Regression tests for the authoritative Pyrefly validation contract."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
TYPECHECK_REQUIREMENTS = ROOT / "requirements-typecheck.txt"
BASELINE = ROOT / "pyrefly-baseline.json"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
BASELINE_WORKFLOW = ROOT / ".github" / "workflows" / "pyrefly-baseline.yml"
LOCAL_AGENT_CONTRACT = ROOT / "references" / "local-agent-validation.md"


def test_pyrefly_is_exactly_pinned_and_configured():
    requirements = TYPECHECK_REQUIREMENTS.read_text(encoding="utf-8")
    assert "pyrefly==1.1.1" in requirements.splitlines()

    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["pyrefly"]
    assert config["baseline"] == "pyrefly-baseline.json"
    assert config["python-version"] == "3.12"
    assert config["python-platform"] == "linux"
    assert config["search-path"] == ["scripts", "src"]
    assert config["project-includes"] == ["scripts/**/*.py", "src/**/*.py"]
    assert config["project-excludes"] == [
        "scripts/test_*.py",
        "scripts/**/test_*.py",
    ]


def test_checked_in_baseline_is_structured_debt_not_inline_suppression():
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert isinstance(payload.get("errors"), list)
    assert payload["errors"]
    for error in payload["errors"]:
        assert isinstance(error.get("path"), str)
        assert isinstance(error.get("name"), str)
        assert error.get("severity") == "error"


def test_ci_runs_read_only_pyrefly_and_binds_release_evidence():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "\n  typecheck:\n    name: Pyrefly\n" in workflow
    assert "run: pyrefly check --output-format=github" in workflow
    assert "--update-baseline" not in workflow
    assert "- typecheck" in workflow
    assert 'needs.typecheck.result }}"' in workflow
    assert '--typecheck-result "${{ needs.typecheck.result }}"' in workflow


def test_baseline_regeneration_is_manual_only():
    workflow = BASELINE_WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert "push:" not in workflow
    assert "contents: read" in workflow
    assert "--update-baseline" in workflow
    assert "actions/upload-artifact@v4" in workflow


def test_local_agent_contract_requires_bounded_and_full_validation():
    contract = LOCAL_AGENT_CONTRACT.read_text(encoding="utf-8")
    for required in (
        "Ruff on changed Python code",
        "Pyrefly on changed Python scope",
        "Focused pytest",
        "Full-project Pyrefly",
        "Relevant broader tests",
        'ruff check "${CHANGED_PY[@]}"',
        'pyrefly check "${CHANGED_PY[@]}"',
        "pyrefly check\n",
    ):
        assert required in contract
