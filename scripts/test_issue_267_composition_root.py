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
_ALLOWED_COMPOSITION_IMPORTERS = {
    "acquisition/direct_scrape.py",
    "container.py",
    "index_admin.py",
    "orchestration/composition.py",
    "store_runtime.py",
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


def _composition_imports(path: Path) -> list[str]:
    modules: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in {"composition", "research_store.composition"}:
                modules.append(node.module)
            elif node.module is None and any(
                alias.name == "composition" for alias in node.names
            ):
                modules.append("")
        elif isinstance(node, ast.Import):
            modules.extend(
                alias.name
                for alias in node.names
                if alias.name in {"composition", "research_store.composition"}
            )
    return modules


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

    assert _composition_imports(facade)
    assert _composition_imports(application) == []

    application_tree = ast.parse(application.read_text(encoding="utf-8"))
    assert any(
        isinstance(node, ast.ClassDef) and node.name == "DirectScrapeService"
        for node in application_tree.body
    )
    facade_tree = ast.parse(facade.read_text(encoding="utf-8"))
    assert not any(isinstance(node, ast.ClassDef) for node in facade_tree.body)


def test_orchestration_legacy_surface_reexports_canonical_root() -> None:
    from research_store.orchestration import composition as legacy

    assert (
        legacy.build_production_orchestrator
        is composition.build_production_orchestrator
    )
    assert (
        legacy.build_production_resumable_orchestrator
        is composition.build_production_resumable_orchestrator
    )
    assert (
        legacy.ProductionBoundedExtractionStage
        is composition.ProductionBoundedExtractionStage
    )


def test_only_explicit_facades_and_operator_wiring_import_composition_root() -> None:
    importers: set[str] = set()
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(_PACKAGE_ROOT).as_posix()
        if relative == "composition.py":
            continue
        if _composition_imports(path):
            importers.add(relative)

    assert importers == _ALLOWED_COMPOSITION_IMPORTERS


def test_composition_root_contains_wiring_not_persistence_or_policy_logic() -> None:
    path = _PACKAGE_ROOT / "composition.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    assert ".execute(" not in source
    assert ".cursor(" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert not any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(
            token in node.value.upper()
            for token in ("SELECT ", "INSERT ", "UPDATE ", "DELETE ")
        )
        for node in ast.walk(tree)
    )

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name.startswith("build_"), node.name
        elif isinstance(node, ast.ClassDef):
            assert node.name == "ProductionBoundedExtractionStage"
            methods = [
                item.name for item in node.body if isinstance(item, ast.FunctionDef)
            ]
            assert methods == ["__init__"]
