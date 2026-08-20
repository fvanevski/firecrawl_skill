from __future__ import annotations

import ast
import importlib
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[2]
RESEARCH_STORE = ROOT / "src" / "firecrawl_skill" / "research_store"

LEGACY_RELEASE_PATHS = (
    Path("benchmark_admin.py"),
    Path("preflight.py"),
    Path("release_benchmark.py"),
    Path("release_evidence.py"),
    Path("strict_benchmark.py"),
    Path("workflow_benchmark.py"),
)

CANONICAL_IMPLEMENTATION_PATHS = (
    "src/firecrawl_skill/research_store/release/preflight.py",
    "src/firecrawl_skill/research_store/release/benchmark.py",
    "src/firecrawl_skill/research_store/release/evidence.py",
    "src/firecrawl_skill/research_store/release/strict.py",
    "src/firecrawl_skill/research_store/release/workflow.py",
)

CANONICAL_MODULES = (
    "firecrawl_skill.research_store.release.admin",
    "firecrawl_skill.research_store.release.benchmark",
    "firecrawl_skill.research_store.release.evidence",
    "firecrawl_skill.research_store.release.preflight",
    "firecrawl_skill.research_store.release.strict",
    "firecrawl_skill.research_store.release.workflow",
)

EXPECTED_CANONICAL_DEFINITIONS = {
    Path("release/admin.py"): {"run_campaign"},
    Path("release/benchmark.py"): {"MetricEngine", "ReleaseBenchmarkRunner"},
    Path("release/evidence.py"): {"ReleaseEvidenceGenerator"},
    Path("release/preflight.py"): {"run_complete_preflight"},
    Path("release/strict.py"): {"main"},
    Path("release/workflow.py"): {"WorkflowBenchmarkRunner"},
}


def test_canonical_release_modules_use_final_module_identity() -> None:
    for module_name in CANONICAL_MODULES:
        module = importlib.import_module(module_name)
        assert module.__name__ == module_name


def test_legacy_release_paths_are_physically_absent() -> None:
    remaining = [
        str(relative)
        for relative in LEGACY_RELEASE_PATHS
        if (RESEARCH_STORE / relative).exists()
    ]
    assert remaining == []


def test_release_package_is_in_distribution_mapping() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools = config["tool"]["setuptools"]
    package_name = "firecrawl_skill.research_store.release"
    assert package_name in setuptools["packages"]
    base = setuptools["package-dir"]["firecrawl_skill"]
    release_dir = ROOT / base / "research_store" / "release"
    assert release_dir.is_dir(), (
        f"release package must resolve under the firecrawl_skill mapping: {release_dir}"
    )


def test_canonical_release_implementations_are_not_pyrefly_baselined() -> None:
    baseline = (ROOT / "pyrefly-baseline.json").read_text(encoding="utf-8")
    for path in CANONICAL_IMPLEMENTATION_PATHS:
        assert path not in baseline, (
            f"canonical implementation must not be baselined: {path}"
        )


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


def test_ordinary_runtime_imports_release_only_through_explicit_cli_boundary() -> None:
    allowed = {Path("cli/benchmark.py")}
    users: set[Path] = set()

    for path in RESEARCH_STORE.rglob("*.py"):
        relative = path.relative_to(RESEARCH_STORE)
        if relative.parts[0] == "release":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _tree_imports_release(tree):
            users.add(relative)

    assert users == allowed
