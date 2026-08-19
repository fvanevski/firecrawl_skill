from __future__ import annotations

import ast
import importlib
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
RESEARCH_STORE = ROOT / "scripts" / "research_store"

BASELINE_TRACKED_RELEASE_PATHS = (
    "scripts/research_store/preflight.py",
    "scripts/research_store/release_benchmark.py",
    "scripts/research_store/release_evidence.py",
    "scripts/research_store/strict_benchmark.py",
    "scripts/research_store/workflow_benchmark.py",
)

CANONICAL_BRIDGES = (
    ("research_store.release_benchmark", "research_store.release.benchmark"),
    ("research_store.release_evidence", "research_store.release.evidence"),
    ("research_store.preflight", "research_store.release.preflight"),
    ("research_store.strict_benchmark", "research_store.release.strict"),
    ("research_store.workflow_benchmark", "research_store.release.workflow"),
)


def test_canonical_release_bridges_preserve_module_identity() -> None:
    for legacy_name, canonical_name in CANONICAL_BRIDGES:
        legacy = importlib.import_module(legacy_name)
        canonical = importlib.import_module(canonical_name)
        assert canonical is legacy, f"{canonical_name} must alias {legacy_name}"


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


def test_baseline_tracked_release_implementations_keep_reviewed_paths() -> None:
    baseline = (ROOT / "pyrefly-baseline.json").read_text(encoding="utf-8")
    for path in BASELINE_TRACKED_RELEASE_PATHS:
        assert path in baseline, (
            f"reviewed Pyrefly debt path unexpectedly moved: {path}"
        )


def _imports_release_package(module_name: str) -> bool:
    return "release" in module_name.split(".")


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
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(_imports_release_package(alias.name) for alias in node.names):
                    users.add(relative)
            elif isinstance(node, ast.ImportFrom):
                module_name = node.module or ""
                if _imports_release_package(module_name):
                    users.add(relative)

    assert users == allowed
