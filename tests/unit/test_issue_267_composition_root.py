"""Final composition-root regressions after Phase-5 gate remediation."""

from __future__ import annotations

import ast
import tomllib
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from firecrawl_skill.research_store import composition, index_admin, store_runtime
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.postgres import PostgresUnitOfWork

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "src" / "firecrawl_skill" / "research_store"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PROFILE_AUTHORITY = ROOT / "ci" / "test-profiles.toml"
_UOW_FIELDS = (
    "database_url",
    "physical_collection",
    "embedding_model",
    "embedding_revision",
    "embedding_dimension",
    "parser_version",
    "normalization_version",
    "chunker_version",
)
_FORBIDDEN_POLICY_CALLS = {
    "begin",
    "commit",
    "complete",
    "cursor",
    "evaluate_post_extraction",
    "evaluate_pre_extraction",
    "execute",
    "execute_search",
    "persist_ingest",
    "prepare_ingest",
    "record_rankings",
    "retry_failed",
    "rollback",
    "run",
    "status",
    "transition",
}
_APPLICATION_MODULES_WITHOUT_ROOT = (
    "orchestrator.py",
    "checkpoint_orchestrator.py",
    "search_provenance.py",
    "run_service.py",
    "fscrape_service.py",
    "fsearch_service.py",
    "fsearch_policy_service.py",
    "inspection_service.py",
    "smart_search_application.py",
    "acquisition/direct_scrape_application.py",
)
_ROOT_OWNED_BUILDERS = {
    "build_fscrape_service",
    "build_fsearch_service",
    "build_policy_fsearch_service",
    "build_inspection_service",
    "build_orchestrator_instance",
    "build_production_orchestrator",
    "build_production_resumable_orchestrator",
}
_ORCHESTRATOR_CLASSES_WITHOUT_BUILDERS = {
    "ResearchOrchestrator",
    "CheckpointResearchOrchestrator",
    "ProvenanceResumableResearchOrchestrator",
}


def _config_stub() -> StoreConfig:
    return cast(
        StoreConfig,
        SimpleNamespace(
            database_url="postgresql://example.invalid/research",
            physical_collection="research_chunks_test",
            embedding_model="embedding-model",
            embedding_revision="revision-1",
            embedding_dimension=1024,
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            chunker_version="structural-v1",
        ),
    )


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _forbidden_calls(tree: ast.AST) -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _FORBIDDEN_POLICY_CALLS
        ):
            result.append((node.func.attr, node.lineno))
    return result


def _imports_name(path: Path, name: str) -> bool:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import) and any(
            alias.name == name or alias.name.endswith(f".{name}")
            for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == name or module.endswith(f".{name}"):
                return True
            if any(alias.name == name for alias in node.names):
                return True
    return False


def _references_name(path: Path, name: str) -> bool:
    return any(
        isinstance(node, ast.Name) and node.id == name for node in ast.walk(_tree(path))
    )


def _top_level_function_names(path: Path) -> set[str]:
    return {
        node.name
        for node in _tree(path).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_build_uow_factory_preserves_exact_constructor_contract() -> None:
    config = _config_stub()
    factory = composition.build_uow_factory(config)
    assert isinstance(factory, partial)
    assert factory.func is PostgresUnitOfWork
    assert factory.args == tuple(getattr(config, name) for name in _UOW_FIELDS)
    assert factory.keywords == {}


def test_orchestration_ci_uses_central_toolchain_before_profile_execution() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    profiles_job = workflow.split("\n  profiles:\n", 1)[1].split(
        "\n  merge-gate:\n", 1
    )[0]
    assert profiles_job.index("Install canonical CI toolchain") < profiles_job.index(
        "Run selected profile"
    )
    authority = tomllib.loads(PROFILE_AUTHORITY.read_text(encoding="utf-8"))
    orchestration = authority["profiles"]["orchestration"]
    assert authority["python_version"] == "3.12"
    assert set(orchestration["services"]) == {"postgres", "qdrant"}
    assert "orchestration" in orchestration["ownership_tokens"]


def test_equivalent_uow_partial_exists_only_in_composition_root() -> None:
    matches: list[str] = []
    for path in sorted(STORE.rglob("*.py")):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "partial":
                continue
            if not node.args or not isinstance(node.args[0], ast.Name):
                continue
            if node.args[0].id == "PostgresUnitOfWork" and len(node.args) == 9:
                matches.append(path.relative_to(STORE).as_posix())
    assert matches == ["composition.py"]


def test_retained_operator_helpers_reuse_canonical_uow_factory() -> None:
    assert store_runtime.uow_factory is composition.build_uow_factory
    assert index_admin.uow_factory is composition.build_uow_factory


def test_migration_composition_facades_are_absent() -> None:
    obsolete = (
        STORE / "container.py",
        STORE / "orchestration" / "composition.py",
        STORE / "acquisition" / "direct_scrape.py",
    )
    assert [
        path.relative_to(STORE).as_posix() for path in obsolete if path.exists()
    ] == []


def test_application_modules_do_not_depend_back_on_composition_root() -> None:
    violations = [
        relative
        for relative in _APPLICATION_MODULES_WITHOUT_ROOT
        if _imports_name(STORE / relative, "composition")
    ]
    assert violations == []


def test_direct_scrape_application_has_no_provider_or_composition_back_edge() -> None:
    path = STORE / "acquisition" / "direct_scrape_application.py"
    source = path.read_text(encoding="utf-8")
    assert not _imports_name(path, "composition")
    assert "FirecrawlDirectScrapeAdapter" not in source


def test_production_builders_exist_only_in_canonical_composition_root() -> None:
    owners: dict[str, list[str]] = {name: [] for name in _ROOT_OWNED_BUILDERS}
    for path in sorted(STORE.rglob("*.py")):
        names = _top_level_function_names(path)
        for name in _ROOT_OWNED_BUILDERS & names:
            owners[name].append(path.relative_to(STORE).as_posix())
    assert owners == {name: ["composition.py"] for name in _ROOT_OWNED_BUILDERS}


def test_orchestrator_application_classes_have_no_config_driven_build_facades() -> None:
    found: list[str] = []
    for relative in (
        "orchestrator.py",
        "checkpoint_orchestrator.py",
        "search_provenance.py",
    ):
        for node in _tree(STORE / relative).body:
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name not in _ORCHESTRATOR_CLASSES_WITHOUT_BUILDERS:
                continue
            for member in node.body:
                if (
                    isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and member.name == "build"
                ):
                    found.append(f"{relative}:{node.name}.build")
    assert found == []


def test_callers_do_not_use_removed_orchestrator_build_facades() -> None:
    """No executable caller may depend on the deleted class-level builders."""
    paths = list(STORE.rglob("*.py")) + list((ROOT / "tests").rglob("*.py"))
    smart_entrypoint = ROOT / "scripts" / "fsearch_smart"
    if smart_entrypoint.is_file():
        paths.append(smart_entrypoint)

    violations: list[str] = []
    for path in sorted(paths):
        for node in ast.walk(_tree(path)):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "build"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in _ORCHESTRATOR_CLASSES_WITHOUT_BUILDERS
            ):
                continue
            violations.append(
                f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:"
                f"{node.func.value.id}.build"
            )
    assert violations == []


def test_production_topology_is_narrow_leaf_wiring() -> None:
    path = STORE / "production_topology.py"
    tree = _tree(path)
    assert not _imports_name(path, "composition")
    assert not _references_name(path, "StoreConfig")
    assert not _references_name(path, "PostgresUnitOfWork")
    assert not _references_name(path, "CorpusService")
    assert _forbidden_calls(tree) == []
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    assert [node.name for node in classes] == ["ProductionBoundedExtractionStage"]


def test_composition_root_contains_wiring_not_persistence_or_policy() -> None:
    path = STORE / "composition.py"
    tree = _tree(path)
    assert _forbidden_calls(tree) == []
    assert not any(isinstance(node, ast.ClassDef) for node in tree.body)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name.startswith("build_")
    assert not any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(
            token in node.value.upper()
            for token in ("SELECT ", "INSERT ", "UPDATE ", "DELETE ")
        )
        for node in ast.walk(tree)
    )
