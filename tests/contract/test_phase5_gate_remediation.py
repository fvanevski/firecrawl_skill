"""Phase-5 gate-remediation contracts.

These regressions close the release-evidence package-boundary failure and ensure
extensionless Python operator entrypoints participate in the final compatibility,
static-validation, and production-ownership authorities.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
STORE = ROOT / "src" / "firecrawl_skill" / "research_store"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release-campaign.yml"
PROFILE_AUTHORITY = ROOT / "ci" / "test-profiles.toml"
PROFILE_RUNNER = SCRIPTS / "run_ci_profile.py"
FSEARCH_SMART = SCRIPTS / "fsearch_smart"
SMART_SEARCH_APPLICATION = STORE / "smart_search_application.py"
FINAL_TOPOLOGY_TEST = ROOT / "tests" / "contract" / "test_issue_269_final_topology.py"
METRICS_TOOL = ROOT / "tools" / "phase5_architecture_metrics.py"
BASELINE = ROOT / "references" / "architecture-baseline.json"


def _is_extensionless_python(path: Path) -> bool:
    if not path.is_file() or path.suffix:
        return False
    try:
        first_line = path.read_bytes().splitlines()[0].lower()
    except (OSError, IndexError):
        return False
    return first_line.startswith(b"#!") and b"python" in first_line


def _extensionless_python_entrypoints() -> list[Path]:
    return sorted(
        path
        for path in SCRIPTS.rglob("*")
        if _is_extensionless_python(path) and "fixtures" not in path.parts
    )


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _absolute_imports(path: Path) -> list[str]:
    imports: list[str] = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.append(node.module)
    return imports


def _defined_functions(path: Path) -> set[str]:
    return {
        node.name
        for node in _tree(path).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _final_topology_forbidden_modules() -> tuple[str, ...]:
    """Read #269's existing authoritative forbidden-module inventory by AST."""
    tree = _tree(FINAL_TOPOLOGY_TEST)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "FORBIDDEN_MODULES"
            for target in node.targets
        ):
            continue
        value = ast.literal_eval(node.value)
        if not isinstance(value, tuple) or not all(
            isinstance(item, str) for item in value
        ):
            raise AssertionError("FORBIDDEN_MODULES is not a tuple[str, ...]")
        return value
    raise AssertionError("final-topology FORBIDDEN_MODULES inventory is missing")


def _matches_forbidden_module(name: str, forbidden_modules: tuple[str, ...]) -> bool:
    return any(
        name == removed or name.startswith(removed + ".")
        for removed in forbidden_modules
    )


def test_extensionless_python_entrypoints_are_in_final_reference_audit() -> None:
    entrypoints = _extensionless_python_entrypoints()
    assert FSEARCH_SMART in entrypoints
    forbidden_modules = _final_topology_forbidden_modules()
    violations: list[str] = []
    for path in entrypoints:
        for module in _absolute_imports(path):
            if _matches_forbidden_module(module, forbidden_modules):
                violations.append(f"{path.relative_to(ROOT)} imports {module}")
    assert violations == [], "removed compatibility imports remain:\n" + "\n".join(
        violations
    )


def test_fsearch_smart_is_policy_free_compatibility_delegate() -> None:
    source = FSEARCH_SMART.read_text(encoding="utf-8")
    imports = _absolute_imports(FSEARCH_SMART)
    assert all(not module.startswith("firecrawl_skill") for module in imports)
    assert 'with_name("fresearch")' in source
    assert "os.execv" in source
    assert '"run", *args' in source
    for forbidden in (
        "build_run_service",
        "build_production_resumable_orchestrator",
        "require_authoritative_acquisition",
        "initialize_planning_bundle",
        "candidate-budget",
        "--research-run-id",
    ):
        assert forbidden not in source
    forbidden_modules = _final_topology_forbidden_modules()
    assert [
        module
        for module in imports
        if _matches_forbidden_module(module, forbidden_modules)
    ] == []


def test_fsearch_smart_contains_no_reusable_application_behavior() -> None:
    functions = _defined_functions(FSEARCH_SMART)
    assert functions == {"main"}
    tree = _tree(FSEARCH_SMART)
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "append_event", "run_from_external_id"}
        for node in ast.walk(tree)
    )
    application_functions = _defined_functions(SMART_SEARCH_APPLICATION)
    assert {
        "canonical_plan",
        "evaluate_budget",
        "initialize_planning_bundle",
        "persist_planner_provenance",
        "plan_queries",
    } <= application_functions
    assert "firecrawl_skill.research_store.composition" not in _absolute_imports(
        SMART_SEARCH_APPLICATION
    )


def test_extensionless_python_is_owned_by_ruff_and_pyrefly_ci() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    runner = PROFILE_RUNNER.read_text(encoding="utf-8")
    assert "scripts/run_ci_profile.py" in workflow
    assert 'EXTENSIONLESS_STATIC_TARGETS = ("scripts/fsearch_smart",)' in runner
    assert runner.count("EXTENSIONLESS_STATIC_TARGETS") >= 5
    assert '["ruff", "check", "--select", "I", *EXTENSIONLESS_STATIC_TARGETS]' in runner
    assert 'run(["pyrefly", "check", "--output-format=github"], cwd=repo)' in runner


def test_release_evidence_installs_canonical_package_before_generator() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    install = workflow.index("python -m pip install --no-deps -e .")
    execute = workflow.index("python scripts/run_release_campaign.py")
    assert install < execute
    assert "PYTHONPATH: scripts" not in workflow


def test_gate_remediation_contract_is_explicitly_ci_owned() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    authority = tomllib.loads(PROFILE_AUTHORITY.read_text(encoding="utf-8"))
    assert "scripts/ci_plan.py" in workflow
    assert "scripts/run_ci_profile.py" in workflow
    assert (
        "tests/contract/test_phase5_gate_remediation.py"
        in authority["profiles"]["tooling"]["selectors"]
    )


def test_phase5_structural_comparison_is_deterministic_and_evidence_only() -> None:
    source_sha = "a" * 40
    command = [
        sys.executable,
        str(METRICS_TOOL),
        "--root",
        str(ROOT),
        "--baseline",
        str(BASELINE),
        "--source-sha",
        source_sha,
    ]
    first = subprocess.run(command, check=True, capture_output=True, text=True)
    second = subprocess.run(command, check=True, capture_output=True, text=True)
    assert first.stdout == second.stdout

    payload = json.loads(first.stdout)
    assert payload["schema_version"] == "phase5-architecture-comparison-v1"
    assert payload["current"]["source_sha"] == source_sha
    assert payload["evidence_policy"].startswith("review evidence only")
    assert payload["baseline"]["source_sha"] != source_sha
    assert payload["baseline"]["summary"]["module_count"] > 0
    assert payload["current"]["summary"]["module_count"] > 0
    assert "scope_transition" in payload["interpretation"]
