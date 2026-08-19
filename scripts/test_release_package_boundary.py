from __future__ import annotations

import ast
import importlib
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
RESEARCH_STORE = ROOT / "scripts" / "research_store"

LEGACY_IMPLEMENTATION_PATHS = (
    Path("preflight.py"),
    Path("release_benchmark.py"),
    Path("release_evidence.py"),
    Path("strict_benchmark.py"),
    Path("workflow_benchmark.py"),
)

CANONICAL_IMPLEMENTATION_PATHS = (
    "scripts/research_store/release/preflight.py",
    "scripts/research_store/release/benchmark.py",
    "scripts/research_store/release/evidence.py",
    "scripts/research_store/release/strict.py",
    "scripts/research_store/release/workflow.py",
)

CANONICAL_BRIDGES = (
    ("research_store.release_benchmark", "research_store.release.benchmark"),
    ("research_store.release_evidence", "research_store.release.evidence"),
    ("research_store.preflight", "research_store.release.preflight"),
    ("research_store.strict_benchmark", "research_store.release.strict"),
    ("research_store.workflow_benchmark", "research_store.release.workflow"),
)

EXPECTED_CANONICAL_DEFINITIONS = {
    Path("release/admin.py"): {"run_campaign"},
    Path("release/benchmark.py"): {"MetricEngine", "ReleaseBenchmarkRunner"},
    Path("release/evidence.py"): {"ReleaseEvidenceGenerator"},
    Path("release/preflight.py"): {"run_complete_preflight"},
    Path("release/strict.py"): {"main"},
    Path("release/workflow.py"): {"WorkflowBenchmarkRunner"},
}


def test_canonical_release_bridges_preserve_module_identity() -> None:
    for legacy_name, canonical_name in CANONICAL_BRIDGES:
        legacy = importlib.import_module(legacy_name)
        canonical = importlib.import_module(canonical_name)
        assert legacy is canonical, f"{legacy_name} must alias {canonical_name}"


def test_benchmark_admin_legacy_path_aliases_canonical_implementation() -> None:
    legacy = importlib.import_module("research_store.benchmark_admin")
    canonical = importlib.import_module("research_store.release.admin")
    assert legacy is canonical


def test_release_package_is_in_distribution_mapping() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools = config["tool"]["setuptools"]
    package_name = "firecrawl_skill.research_store.release"
    assert package_name in setuptools["packages"]
    assert setuptools["package-dir"][package_name] == "scripts/research_store/release"


def test_canonical_release_implementations_are_not_pyrefly_baselined() -> None:
    baseline = (ROOT / "pyrefly-baseline.json").read_text(encoding="utf-8")
    for path in CANONICAL_IMPLEMENTATION_PATHS:
        assert path not in baseline, f"canonical implementation must not be baselined: {path}"


def test_legacy_release_modules_are_zero_domain_logic_facades() -> None:
    for relative in LEGACY_IMPLEMENTATION_PATHS:
        path = RESEARCH_STORE / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        definitions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        assert not definitions, f"legacy release facade contains implementation: {relative}"


def test_canonical_release_modules_own_expected_definitions() -> None:
    for relative, expected in EXPECTED_CANONICAL_DEFINITIONS.items():
        path = RESEARCH_STORE / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        definitions = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        assert expected <= definitions, (
            f"canonical release module {relative} does not own {sorted(expected - definitions)}"
        )


def _module_mentions_release(module_name: str) -> bool:
    return "release" in module_name.split(".")


def _tree_imports_release(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(_module_mentions_release(alias.name) for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and _module_mentions_release(node.module):
                return True
            if (
                node.level > 0
                and node.module is None
                and any(alias.name == "release" for alias in node.names)
            ):
                return True
    return False


def test_release_import_detector_covers_relative_import_forms() -> None:
    examples = (
        "import research_store.release\n",
        "from research_store.release import strict\n",
        "from .release import strict\n",
        "from . import release\n",
        "from .. import release\n",
    )
    for source in examples:
        assert _tree_imports_release(ast.parse(source)), source


def test_ordinary_runtime_does_not_import_release_package() -> None:
    allowed = {
        Path("benchmark_admin.py"),
        Path("cli/benchmark.py"),
    }
    users: set[Path] = set()

    for path in RESEARCH_STORE.rglob("*.py"):
        relative = path.relative_to(RESEARCH_STORE)
        if relative.parts[0] == "release":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _tree_imports_release(tree):
            users.add(relative)

    assert users == allowed
