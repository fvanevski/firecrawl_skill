"""Final composition-root regressions after issue #269 compatibility cleanup."""

from __future__ import annotations

import ast
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from firecrawl_skill.research_store import composition, index_admin, store_runtime
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.postgres import PostgresUnitOfWork

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "src" / "firecrawl_skill" / "research_store"
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


def test_build_uow_factory_preserves_exact_constructor_contract() -> None:
    config = _config_stub()
    factory = composition.build_uow_factory(config)
    assert isinstance(factory, partial)
    assert factory.func is PostgresUnitOfWork
    assert factory.args == tuple(getattr(config, name) for name in _UOW_FIELDS)
    assert factory.keywords == {}


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


def test_direct_scrape_application_has_no_composition_back_edge() -> None:
    path = STORE / "acquisition" / "direct_scrape_application.py"
    source = path.read_text(encoding="utf-8")
    assert not _imports_name(path, "composition")
    assert "FirecrawlDirectScrapeAdapter" not in source


def test_historical_orchestrator_builders_use_leaf_topology_not_composition() -> None:
    for relative in ("checkpoint_orchestrator.py", "search_provenance.py"):
        path = STORE / relative
        source = path.read_text(encoding="utf-8")
        assert (
            "from .production_topology import ProductionBoundedExtractionStage"
            in source
        )
        assert not _imports_name(path, "composition")


def test_production_topology_is_narrow_leaf_wiring() -> None:
    path = STORE / "production_topology.py"
    source = path.read_text(encoding="utf-8")
    tree = _tree(path)
    assert not _imports_name(path, "composition")
    assert "StoreConfig" not in source
    assert "PostgresUnitOfWork" not in source
    assert "CorpusService" not in source
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
