"""Phase-5 gate-remediation contracts.

These regressions close the release-evidence package-boundary failure and ensure
extensionless Python operator entrypoints participate in the final compatibility
reference audit.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
FSEARCH_SMART = SCRIPTS / "fsearch_smart"
FINAL_TOPOLOGY_TEST = ROOT / "tests" / "contract" / "test_issue_269_final_topology.py"
METRICS_TOOL = ROOT / "tools" / "phase5_architecture_metrics.py"
BASELINE = ROOT / "references" / "architecture-baseline.json"
FSEARCH_REMOVED_OWNERS = (
    "firecrawl_skill.research_store.acquisition_authority",
    "firecrawl_skill.research_store.container",
    "firecrawl_skill.research_store.orchestration.composition",
)


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


def _absolute_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.append(node.module)
    return imports


def _final_topology_forbidden_modules() -> tuple[str, ...]:
    """Read #269's existing authoritative forbidden-module inventory by AST."""
    tree = ast.parse(
        FINAL_TOPOLOGY_TEST.read_text(encoding="utf-8"),
        filename=str(FINAL_TOPOLOGY_TEST),
    )
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


def test_extensionless_python_entrypoints_are_in_final_reference_audit() -> None:
    entrypoints = _extensionless_python_entrypoints()
    assert FSEARCH_SMART in entrypoints
    forbidden_modules = _final_topology_forbidden_modules()
    violations: list[str] = []
    for path in entrypoints:
        for module in _absolute_imports(path):
            if any(
                module == removed or module.startswith(removed + ".")
                for removed in forbidden_modules
            ):
                violations.append(f"{path.relative_to(ROOT)} imports {module}")
    assert violations == [], "removed compatibility imports remain:\n" + "\n".join(
        violations
    )


def test_fsearch_smart_uses_final_phase5_owners() -> None:
    source = FSEARCH_SMART.read_text(encoding="utf-8")
    imports = _absolute_imports(FSEARCH_SMART)
    assert "firecrawl_skill.research_store.acquisition.authority" in imports
    assert "firecrawl_skill.research_store.composition" in imports
    assert "require_authoritative_acquisition" in source
    assert "build_run_service" in source
    assert "build_production_resumable_orchestrator" in source
    for removed in FSEARCH_REMOVED_OWNERS:
        assert removed not in source


def test_release_evidence_installs_canonical_package_before_generator() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    section = workflow.split("  release-evidence:\n", 1)[1].split(
        "  dispatch-release-campaign:\n", 1
    )[0]
    install = section.index("python -m pip install --no-deps -e .")
    generate = section.index("python scripts/generate_exact_head_ci_evidence.py")
    assert install < generate
    assert "PYTHONPATH: scripts" not in section


def test_gate_remediation_contract_is_explicitly_ci_owned() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    release_section = workflow.split("  release-invariants:\n", 1)[1].split(
        "  test:\n", 1
    )[0]
    assert "tests/contract/test_phase5_gate_remediation.py" in release_section


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
