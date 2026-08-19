from __future__ import annotations

import ast
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from research_store import composition, container, index_admin, store_runtime
from research_store.config import StoreConfig
from research_store.postgres import PostgresUnitOfWork

_PACKAGE_ROOT = Path(__file__).resolve().parent / "research_store"
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
_LEGACY_CONTAINER_BUILDERS = (
    "build_acquisition_service",
    "build_audit_service",
    "build_claim_service",
    "build_evidence_service",
    "build_extraction_service",
    "build_invocation_service",
    "build_orchestrator",
    "build_resource_governor",
    "build_run_service",
    "build_semantic_service",
    "build_service",
    "build_strategy_service",
    "build_workflow_operation_service",
)
_COMPOSITION_MODULES = {
    "composition",
    "orchestration.composition",
    "research_store.composition",
    "research_store.orchestration.composition",
}
_COMPOSITION_ALIAS_PARENTS = {
    "",
    "orchestration",
    "research_store",
    "research_store.orchestration",
}
_ALLOWED_COMPOSITION_IMPORTERS = {
    "acquisition/direct_scrape.py",
    "container.py",
    "index_admin.py",
    "orchestration/__init__.py",
    "orchestration/composition.py",
    "store_runtime.py",
}
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


def _is_equivalent_uow_partial(node: ast.Call) -> bool:
    if not isinstance(node.func, ast.Name) or node.func.id != "partial":
        return False
    if len(node.args) != len(_UOW_FIELDS) + 1:
        return False
    target = node.args[0]
    if not isinstance(target, ast.Name) or target.id != "PostgresUnitOfWork":
        return False
    bound = node.args[1:]
    if not all(isinstance(arg, ast.Attribute) for arg in bound):
        return False
    return (
        tuple(arg.attr for arg in bound if isinstance(arg, ast.Attribute))
        == _UOW_FIELDS
    )


def _composition_surface_imports(path: Path) -> list[str]:
    modules: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in _COMPOSITION_MODULES:
                modules.append(module)
            elif module in _COMPOSITION_ALIAS_PARENTS and any(
                alias.name == "composition" for alias in node.names
            ):
                modules.append(f"{module}:composition")
        elif isinstance(node, ast.Import):
            modules.extend(
                alias.name for alias in node.names if alias.name in _COMPOSITION_MODULES
            )
    return modules


def _forbidden_policy_calls(tree: ast.AST) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in _FORBIDDEN_POLICY_CALLS:
            calls.append((node.func.attr, node.lineno))
    return calls


def test_build_uow_factory_preserves_exact_constructor_contract() -> None:
    config = _config_stub()

    factory = composition.build_uow_factory(config)

    assert isinstance(factory, partial)
    assert factory.func is PostgresUnitOfWork
    assert factory.args == (
        config.database_url,
        config.physical_collection,
        config.embedding_model,
        config.embedding_revision,
        config.embedding_dimension,
        config.parser_version,
        config.normalization_version,
        config.chunker_version,
    )
    assert factory.keywords == {}


def test_equivalent_uow_construction_exists_only_in_canonical_root() -> None:
    locations: list[tuple[str, int]] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_equivalent_uow_partial(node):
                locations.append(
                    (path.relative_to(_PACKAGE_ROOT).as_posix(), node.lineno)
                )

    assert len(locations) == 1
    assert locations[0][0] == "composition.py"


def test_legacy_uow_factory_surfaces_reexport_canonical_factory() -> None:
    assert store_runtime.uow_factory is composition.build_uow_factory
    assert index_admin.uow_factory is composition.build_uow_factory


def test_container_builders_are_thin_canonical_reexports() -> None:
    for name in _LEGACY_CONTAINER_BUILDERS:
        assert getattr(container, name) is getattr(composition, name), name


def test_direct_scrape_builder_delegates_to_canonical_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_store.acquisition import direct_scrape as legacy
    from research_store.acquisition import direct_scrape_application as application

    assert legacy.DirectScrapeService is application.DirectScrapeService
    assert legacy.DirectScrapePersistenceError is application.DirectScrapePersistenceError

    sentinel = object()
    config = cast(StoreConfig, object())
    adapter_factory: Any = lambda: object()
    received: dict[str, object] = {}

    def fake_builder(
        received_config: StoreConfig,
        *,
        adapter_factory: Any = None,
    ) -> object:
        received["config"] = received_config
        received["adapter_factory"] = adapter_factory
        return sentinel

    monkeypatch.setattr(composition, "build_direct_scrape_service", fake_builder)

    result = legacy.build_direct_scrape_service(
        config,
        adapter_factory=adapter_factory,
    )

    assert result is sentinel
    assert received == {"config": config, "adapter_factory": adapter_factory}


def test_direct_scrape_application_has_no_composition_back_edge() -> None:
    facade = _PACKAGE_ROOT / "acquisition" / "direct_scrape.py"
    application = _PACKAGE_ROOT / "acquisition" / "direct_scrape_application.py"

    assert _composition_surface_imports(facade)
    assert _composition_surface_imports(application) == []

    application_tree = ast.parse(application.read_text(encoding="utf-8"))
    assert any(
        isinstance(node, ast.ClassDef) and node.name == "DirectScrapeService"
        for node in application_tree.body
    )
    facade_tree = ast.parse(facade.read_text(encoding="utf-8"))
    assert not any(isinstance(node, ast.ClassDef) for node in facade_tree.body)


def test_historical_orchestrator_builders_do_not_depend_back_on_composition() -> None:
    for relative in ("checkpoint_orchestrator.py", "search_provenance.py"):
        path = _PACKAGE_ROOT / relative
        source = path.read_text(encoding="utf-8")
        assert _composition_surface_imports(path) == [], relative
        assert "from .production_topology import ProductionBoundedExtractionStage" in source


def test_orchestration_legacy_surface_reexports_canonical_root() -> None:
    from research_store.orchestration import composition as legacy
    from research_store.production_topology import ProductionBoundedExtractionStage

    assert (
        legacy.build_production_orchestrator
        is composition.build_production_orchestrator
    )
    assert (
        legacy.build_production_resumable_orchestrator
        is composition.build_production_resumable_orchestrator
    )
    assert legacy.ProductionBoundedExtractionStage is ProductionBoundedExtractionStage
    assert composition.ProductionBoundedExtractionStage is ProductionBoundedExtractionStage


def test_only_explicit_facades_and_operator_wiring_import_composition_surfaces() -> None:
    importers: set[str] = set()
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(_PACKAGE_ROOT).as_posix()
        if relative == "composition.py":
            continue
        if _composition_surface_imports(path):
            importers.add(relative)

    assert importers == _ALLOWED_COMPOSITION_IMPORTERS


def test_production_topology_is_a_leaf_wiring_primitive() -> None:
    path = _PACKAGE_ROOT / "production_topology.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    assert _composition_surface_imports(path) == []
    assert "StoreConfig" not in source
    assert "PostgresUnitOfWork" not in source
    assert "CorpusService" not in source
    assert _forbidden_policy_calls(tree) == []

    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    assert [node.name for node in classes] == ["ProductionBoundedExtractionStage"]
    methods = [
        item.name for item in classes[0].body if isinstance(item, ast.FunctionDef)
    ]
    assert methods == ["__init__"]


def test_composition_root_contains_wiring_not_persistence_or_policy_logic() -> None:
    path = _PACKAGE_ROOT / "composition.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    assert ".execute(" not in source
    assert ".cursor(" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert _forbidden_policy_calls(tree) == []
    assert not any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(
            token in node.value.upper()
            for token in ("SELECT ", "INSERT ", "UPDATE ", "DELETE ")
        )
        for node in ast.walk(tree)
    )

    classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
    assert classes == []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name.startswith("build_"), node.name
