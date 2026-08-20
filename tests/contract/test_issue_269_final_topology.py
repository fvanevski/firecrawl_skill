"""Final compatibility-cleanup architecture contract for issue #269."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
STORE = SRC / "firecrawl_skill" / "research_store"
SCRIPTS = ROOT / "scripts"
SELF = Path(__file__).resolve()
BASELINE = ROOT / "pyrefly-baseline.json"

FORBIDDEN_PATHS = (
    SCRIPTS / "budget_policy.py",
    SCRIPTS / "candidate_ranking.py",
    SCRIPTS / "classifier.py",
    SCRIPTS / "model_gateway.py",
    STORE / "service.py",
    STORE / "container.py",
    STORE / "coverage_service.py",
    STORE / "quality_service.py",
    STORE / "duplicate_service.py",
    STORE / "evidence_grouping.py",
    STORE / "audit_packet.py",
    STORE / "evidence.py",
    STORE / "claim_binding_service.py",
    STORE / "packet_validator.py",
    STORE / "report_service.py",
    STORE / "report_validator.py",
    STORE / "report_artifact_service.py",
    STORE / "acquisition_authority.py",
    STORE / "acquisition_service.py",
    STORE / "bounded_acquisition.py",
    STORE / "direct_scrape_service.py",
    STORE / "acquisition" / "direct_scrape.py",
    STORE / "benchmark_admin.py",
    STORE / "preflight.py",
    STORE / "release_benchmark.py",
    STORE / "release_evidence.py",
    STORE / "strict_benchmark.py",
    STORE / "workflow_benchmark.py",
    STORE / "orchestration" / "composition.py",
    STORE / "cli.py",
    STORE / "retrieval.py",
    STORE / "retrieval_core.py",
    STORE / "retrieval_service.py",
    STORE / "postgres_retrieval.py",
    STORE / "qdrant.py",
    STORE / "qdrant_authority.py",
    STORE / "projection_reconciliation.py",
    STORE / "indexing.py",
    STORE / "checkpoint_indexing_stage.py",
    STORE / "index_checkpoint_asset_membership.py",
    STORE / "index_checkpoint_core.py",
    STORE / "index_checkpoint_finalize.py",
    STORE / "index_checkpoint_models.py",
    STORE / "index_checkpoint_replay.py",
    STORE / "index_checkpoint_service.py",
    STORE / "index_checkpoint_store.py",
)

FORBIDDEN_MODULES = (
    "budget_policy",
    "candidate_ranking",
    "classifier",
    "model_gateway",
    "firecrawl_skill.research_store.service",
    "firecrawl_skill.research_store.container",
    "firecrawl_skill.research_store.coverage_service",
    "firecrawl_skill.research_store.quality_service",
    "firecrawl_skill.research_store.duplicate_service",
    "firecrawl_skill.research_store.evidence_grouping",
    "firecrawl_skill.research_store.audit_packet",
    "firecrawl_skill.research_store.evidence",
    "firecrawl_skill.research_store.claim_binding_service",
    "firecrawl_skill.research_store.packet_validator",
    "firecrawl_skill.research_store.report_service",
    "firecrawl_skill.research_store.report_validator",
    "firecrawl_skill.research_store.report_artifact_service",
    "firecrawl_skill.research_store.acquisition_authority",
    "firecrawl_skill.research_store.acquisition_service",
    "firecrawl_skill.research_store.bounded_acquisition",
    "firecrawl_skill.research_store.direct_scrape_service",
    "firecrawl_skill.research_store.acquisition.direct_scrape",
    "firecrawl_skill.research_store.benchmark_admin",
    "firecrawl_skill.research_store.preflight",
    "firecrawl_skill.research_store.release_benchmark",
    "firecrawl_skill.research_store.release_evidence",
    "firecrawl_skill.research_store.strict_benchmark",
    "firecrawl_skill.research_store.workflow_benchmark",
    "firecrawl_skill.research_store.orchestration.composition",
    "firecrawl_skill.research_store.retrieval_service",
    "firecrawl_skill.research_store.postgres_retrieval",
    "firecrawl_skill.research_store.qdrant",
    "firecrawl_skill.research_store.qdrant_authority",
    "firecrawl_skill.research_store.projection_reconciliation",
    "firecrawl_skill.research_store.indexing",
    "firecrawl_skill.research_store.checkpoint_indexing_stage",
    "firecrawl_skill.research_store.index_checkpoint_asset_membership",
    "firecrawl_skill.research_store.index_checkpoint_core",
    "firecrawl_skill.research_store.index_checkpoint_finalize",
    "firecrawl_skill.research_store.index_checkpoint_models",
    "firecrawl_skill.research_store.index_checkpoint_replay",
    "firecrawl_skill.research_store.index_checkpoint_service",
    "firecrawl_skill.research_store.index_checkpoint_store",
)


def _matches_forbidden_module(name: str) -> bool:
    return any(
        name == item or name.startswith(item + ".") for item in FORBIDDEN_MODULES
    )


def _is_script_fixture(path: Path) -> bool:
    try:
        relative = path.relative_to(SCRIPTS)
    except ValueError:
        return False
    return bool(relative.parts and relative.parts[0] == "fixtures")


def _python_files() -> list[Path]:
    forbidden = {path.resolve() for path in FORBIDDEN_PATHS}
    files = [
        *SRC.rglob("*.py"),
        *(ROOT / "tests").rglob("*.py"),
        *SCRIPTS.rglob("*.py"),
    ]
    return sorted(
        path
        for path in files
        if path.resolve() != SELF
        and path.resolve() not in forbidden
        and not _is_script_fixture(path)
    )


def _module_context(path: Path) -> tuple[str, str] | None:
    try:
        relative = path.relative_to(SRC)
    except ValueError:
        return None
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
        module = ".".join(parts)
        package = module
    else:
        module = ".".join(parts)
        package = ".".join(parts[:-1])
    return module, package


def _resolve_import_from(path: Path, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    context = _module_context(path)
    if context is None:
        return None
    _module, package = context
    parts = package.split(".") if package else []
    ascend = node.level - 1
    if ascend > len(parts):
        return None
    base = parts[: len(parts) - ascend]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _resolved_imported_modules(path: Path, tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = _resolve_import_from(path, node)
            if not module:
                continue
            modules.append(module)
            modules.extend(f"{module}.{alias.name}" for alias in node.names)
    return modules


def _string_targets(tree: ast.AST) -> list[str]:
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_obsolete_compatibility_paths_are_physically_absent() -> None:
    remaining = [
        str(path.relative_to(ROOT)) for path in FORBIDDEN_PATHS if path.exists()
    ]
    assert remaining == [], f"obsolete compatibility paths remain: {remaining}"


def test_source_tests_and_operator_scripts_import_only_final_owners() -> None:
    violations: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _resolved_imported_modules(path, tree):
            if _matches_forbidden_module(module):
                violations.append(f"{path.relative_to(ROOT)} imports {module}")
    assert violations == [], "legacy module imports remain:\n" + "\n".join(violations)


def test_installed_package_never_imports_top_level_script_modules() -> None:
    script_modules = {
        path.stem
        for path in SCRIPTS.glob("*.py")
        if path.is_file() and not path.name.startswith(".")
    }
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _resolved_imported_modules(path, tree):
            top_level = module.split(".", 1)[0]
            if top_level in script_modules:
                violations.append(
                    f"{path.relative_to(ROOT)} imports scripts/{top_level}.py"
                )
    assert violations == [], "installed package depends on scripts/:\n" + "\n".join(
        violations
    )


def test_dynamic_patch_and_import_targets_use_final_owners() -> None:
    violations: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for value in _string_targets(tree):
            for module in FORBIDDEN_MODULES:
                if value == module or value.startswith(module + "."):
                    violations.append(f"{path.relative_to(ROOT)} targets {value}")
                    break
    assert violations == [], "legacy runtime module targets remain:\n" + "\n".join(
        violations
    )


def test_scripts_contains_no_pytest_test_modules() -> None:
    test_modules = sorted(
        str(path.relative_to(ROOT))
        for path in SCRIPTS.rglob("test_*.py")
        if not _is_script_fixture(path)
    )
    assert test_modules == [], f"production tests remain under scripts/: {test_modules}"


def test_script_fixtures_remain_valid_without_legacy_implementation_targets() -> None:
    classifier = SCRIPTS / "fixtures" / "classifier.py"
    model_gateway = SCRIPTS / "fixtures" / "model_gateway.py"

    assert classifier.is_file()
    assert not classifier.is_symlink()
    classifier_source = classifier.read_text(encoding="utf-8")
    assert "firecrawl_skill.research_store.acquisition.classifier" in classifier_source
    assert "../classifier.py" not in classifier_source

    assert model_gateway.is_symlink()
    assert model_gateway.is_file(), "model-gateway fixture symlink is dangling"
    assert (
        model_gateway.resolve()
        == (SRC / "firecrawl_skill" / "model_gateway.py").resolve()
    )


def test_setuptools_has_no_scripts_production_module_root() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools = config["tool"]["setuptools"]
    assert "py-modules" not in setuptools
    assert "" not in setuptools["package-dir"]
    assert setuptools["package-dir"]["firecrawl_skill"] == "src/firecrawl_skill"


def test_pyrefly_baseline_references_only_maintained_paths() -> None:
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    errors = data["errors"]
    missing = sorted(
        {
            str(item["path"])
            for item in errors
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and not (ROOT / item["path"]).exists()
        }
    )
    assert missing == [], f"stale Pyrefly baseline paths remain: {missing}"
    forbidden = {path.relative_to(ROOT).as_posix() for path in FORBIDDEN_PATHS}
    retained_forbidden = sorted(
        {
            str(item["path"])
            for item in errors
            if isinstance(item, dict) and item.get("path") in forbidden
        }
    )
    assert retained_forbidden == []


def test_package_root_exposes_no_migration_adapter_aliases() -> None:
    source = (STORE / "__init__.py").read_text(encoding="utf-8")
    assert "FirecrawlSearchAdapter" not in source
    ports = (STORE / "ports.py").read_text(encoding="utf-8")
    assert "from .acquisition.ports import SearchAdapter" not in ports


def test_report_construction_has_one_canonical_owner() -> None:
    path = STORE / "reporting" / "construction.py"
    assert path.is_file()
    source = path.read_text(encoding="utf-8")
    assert "class LocalSynthesisService" in source
    assert "Path(__file__).resolve().parents[4]" in source
    assert not (STORE / "report_service.py").exists()


def test_drain_script_is_operator_launcher_not_implementation_owner() -> None:
    path = SCRIPTS / "drain_index_jobs.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    definitions = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert definitions == []
    assert "firecrawl_skill.research_store.retrieval.projection.drain" in source


def test_workflows_do_not_execute_removed_python_modules_or_paths() -> None:
    violations: list[str] = []
    forbidden_paths = tuple(
        path.relative_to(ROOT).as_posix() for path in FORBIDDEN_PATHS
    )
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        source = path.read_text(encoding="utf-8")
        for module in FORBIDDEN_MODULES:
            if module.startswith("firecrawl_skill.") and module in source:
                violations.append(
                    f"{path.relative_to(ROOT)} references module {module}"
                )
        for legacy_path in forbidden_paths:
            if legacy_path in source:
                violations.append(
                    f"{path.relative_to(ROOT)} references path {legacy_path}"
                )
    assert violations == [], "workflow legacy targets remain:\n" + "\n".join(violations)


def test_non_python_operator_entrypoints_do_not_execute_removed_modules() -> None:
    violations: list[str] = []
    for path in sorted(SCRIPTS.iterdir()):
        if not path.is_file() or path.suffix == ".py":
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for module in FORBIDDEN_MODULES:
            if (
                f"-m {module}" in source
                or f"-m '{module}'" in source
                or f'-m "{module}"' in source
            ):
                violations.append(f"{path.relative_to(ROOT)} executes {module}")
    assert violations == [], (
        "operator entrypoints execute legacy modules:\n" + "\n".join(violations)
    )


def test_ci_typechecks_canonical_model_gateway() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "src/firecrawl_skill/model_gateway.py" in workflow
    assert "scripts/model_gateway.py" not in workflow
