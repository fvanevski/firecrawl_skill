"""Final compatibility-cleanup architecture contract for issue #269."""

from __future__ import annotations

import ast
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
STORE = SRC / "firecrawl_skill" / "research_store"
SCRIPTS = ROOT / "scripts"
SELF = Path(__file__).resolve()

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
    return any(name == item or name.startswith(item + ".") for item in FORBIDDEN_MODULES)


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


def _absolute_imported_modules(tree: ast.AST) -> list[str]:
    """Return absolute imports that can resolve to legacy module identities.

    ``ast.ImportFrom.module`` omits leading dots, so a relative canonical import
    such as ``from ..budget_policy`` appears as module ``budget_policy`` with a
    nonzero ``level``. Only level-zero imports are eligible to resolve to the
    removed top-level script module identity.
    """
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
    return modules


def _string_targets(tree: ast.AST) -> list[str]:
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_obsolete_compatibility_paths_are_physically_absent() -> None:
    remaining = [str(path.relative_to(ROOT)) for path in FORBIDDEN_PATHS if path.exists()]
    assert remaining == [], f"obsolete compatibility paths remain: {remaining}"


def test_source_tests_and_operator_scripts_import_only_final_owners() -> None:
    violations: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _absolute_imported_modules(tree):
            if _matches_forbidden_module(module):
                violations.append(f"{path.relative_to(ROOT)} imports {module}")
    assert violations == [], "legacy module imports remain:\n" + "\n".join(violations)


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


def test_setuptools_has_no_scripts_production_module_root() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools = config["tool"]["setuptools"]
    assert "py-modules" not in setuptools
    assert "" not in setuptools["package-dir"]
    assert setuptools["package-dir"]["firecrawl_skill"] == "src/firecrawl_skill"


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


def test_workflows_do_not_execute_removed_python_modules() -> None:
    violations: list[str] = []
    workflow_modules = tuple(
        module for module in FORBIDDEN_MODULES if module.startswith("firecrawl_skill.")
    )
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        source = path.read_text(encoding="utf-8")
        for module in workflow_modules:
            if module in source:
                violations.append(f"{path.relative_to(ROOT)} references {module}")
    assert violations == [], "workflow legacy module targets remain:\n" + "\n".join(
        violations
    )


def test_ci_typechecks_canonical_model_gateway() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "src/firecrawl_skill/model_gateway.py" in workflow
    assert "scripts/model_gateway.py" not in workflow
