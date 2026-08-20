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
    STORE / "acquisition_service.py",
    STORE / "release_benchmark.py",
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
    "firecrawl_skill.research_store.acquisition_service",
    "firecrawl_skill.research_store.release_benchmark",
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


def _python_files() -> list[Path]:
    files = [*SRC.rglob("*.py"), *(ROOT / "tests").rglob("*.py")]
    return sorted(path for path in files if path.resolve() != SELF)


def _imported_modules(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
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


def test_source_and_tests_import_only_final_owners() -> None:
    violations: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
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


def test_ci_typechecks_canonical_model_gateway() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "src/firecrawl_skill/model_gateway.py" in workflow
    assert "scripts/model_gateway.py" not in workflow
