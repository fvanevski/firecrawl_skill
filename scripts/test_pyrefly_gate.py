"""Regression tests for the authoritative Pyrefly validation contract."""

from __future__ import annotations

import json
from pathlib import Path

import tomllib

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
    assert config["search-path"] == ["scripts", "src", "."]
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


def test_ci_binds_pyrefly_to_exact_candidate_and_validates_negative_probe():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "Check out exact Pyrefly candidate" in workflow
    assert (
        "ref: ${{ inputs.candidate-sha || github.event.pull_request.head.sha || github.sha }}"
        in workflow
    )
    assert "Verify checked-out Pyrefly candidate" in workflow
    assert 'test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"' in workflow
    assert "Verify repository-root import resolution" in workflow
    assert "scripts/model_gateway.py" in workflow
    assert "scripts/fixtures/model_gateway.py" in workflow
    assert "Verify explicit changed-scope Pyrefly enforcement" in workflow
    assert 'pyrefly check "$PROBE" --output-format=github' in workflow
    assert 'if [ "$rc" -ne 1 ]; then' in workflow
    assert 'grep -Fq "$PROBE" "$OUTPUT"' in workflow
    assert 'if [ "$rc" -eq 0 ]; then' not in workflow


def test_baseline_regeneration_is_manual_only_and_fails_closed():
    workflow = BASELINE_WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert "push:" not in workflow
    assert "contents: read" in workflow
    assert "--update-baseline" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "continue-on-error: true" not in workflow
    assert 'case "$rc" in' in workflow
    assert "0|1)" in workflow
    assert 'exit "$rc"' in workflow


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
        "Exact-head and Pyrefly exit-code evidence",
        "exit code `1`",
        "exit codes `3` and `101`",
        "Repository merge-policy invariant",
        "must require the exact `Pyrefly` status-check context",
        "both required and successful",
    ):
        assert required in contract


def test_repository_root_scripts_namespace_resolves_without_baseline_debt():
    """``scripts.*`` in-repository imports resolve under the committed config.

    ``scripts`` is a namespace package below the repository root, so an import
    such as ``scripts.research_store.semantic_service`` only resolves once the
    repo root (``.``) is on the search path. Regression for the missing-root
    defect: the committed baseline must not carry a configuration-caused
    ``missing-import`` for the ``scripts.`` namespace.
    """
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
    """Extract the dotted module name from a ``missing-import`` baseline entry."""
    description = error.get("concise_description")
    if not isinstance(description, str):
        return ""
    marker = "Cannot find module `"
    if not description.startswith(marker) or not description.endswith("`"):
        return ""
    return description[len(marker) : -1]
