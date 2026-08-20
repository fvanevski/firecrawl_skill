"""Regression tests for the authoritative Pyrefly validation contract."""

from __future__ import annotations

import json
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[2]
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


def test_ci_binds_pyrefly_to_exact_candidate_and_validates_changed_scope_and_probe():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "Check out exact Pyrefly candidate" in workflow
    assert (
        "ref: ${{ inputs.candidate-sha || github.event.pull_request.head.sha || github.sha }}"
        in workflow
    )
    assert "fetch-depth: 0" in workflow
    assert "Verify checked-out Pyrefly candidate" in workflow
    assert 'test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"' in workflow
    assert "Run Pyrefly on actual changed Python scope" in workflow
    assert "BASE_SHA: ${{ github.event.pull_request.base.sha }}" in workflow
    assert "HEAD_SHA: ${{ github.event.pull_request.head.sha }}" in workflow
    assert (
        'git diff --name-only --diff-filter=ACMR "$BASE_SHA" "$HEAD_SHA" -- \'*.py\''
        in workflow
    )
    assert 'pyrefly check "${changed_python[@]}" --output-format=github' in workflow
    assert "grep -vE '(^|/)test_[^/]*\\.py$'" not in workflow
    assert "Verify repository-root import resolution" in workflow
    assert "src/firecrawl_skill/model_gateway.py" in workflow
    assert "scripts/model_gateway.py" not in workflow
    # The deterministic fixture remains deliberately outside the installed
    # package and is type-checked as test/support code.
    assert "scripts/fixtures/model_gateway.py" in workflow
    assert "Verify Pyrefly explicit-file diagnostic behavior" in workflow
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
        "Include changed test files explicitly",
        "pyrefly check\n",
        "Exact-head and Pyrefly exit-code evidence",
        "exit code `1`",
        "exit codes `3` and `101`",
        "Repository merge-policy invariant",
        "must require the exact `Pyrefly` status-check context",
        "both required and successful",
    ):
        assert required in contract
    assert "CHANGED_PY_TYPECHECK" not in contract
    assert "grep -vE '(^|/)test_[^/]*\\.py$'" not in contract


def test_operator_scripts_namespace_resolves_without_baseline_configuration_debt():
    """Operator/support code remains type-checked without becoming runtime authority.

    `scripts/` stays on Pyrefly's search path because operator and fixture code
    is checked. Issue #269 separately proves installed `firecrawl_skill.*`
    modules do not import top-level script implementations.
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
    """Extract the dotted module name from a `missing-import` baseline entry."""
    description = error.get("concise_description")
    if not isinstance(description, str):
        return ""
    marker = "Cannot find module `"
    if not description.startswith(marker) or not description.endswith("`"):
        return ""
    return description[len(marker) : -1]
