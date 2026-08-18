"""Issue #262 acquisition vertical-slice boundary regressions."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import research_store
from research_store import (
    acquisition_authority as legacy_acquisition_authority,
)
from research_store import acquisition_service as legacy_acquisition_service
from research_store import bounded_acquisition as legacy_bounded_acquisition
from research_store import direct_scrape_service as legacy_direct_scrape
from research_store import fsearch_service
from research_store.acquisition import (
    authority as canonical_acquisition_authority,
)
from research_store.acquisition.adapters.bounded_firecrawl import (
    BoundedFirecrawlSearchAdapter,
)
from research_store.acquisition.adapters.firecrawl_scrape import (
    FirecrawlDirectScrapeAdapter,
)
from research_store.acquisition.adapters.firecrawl_search import (
    MetadataOnlyFirecrawlSearchAdapter,
)
from research_store.acquisition.authority import AuthoritativeAcquisitionContext
from research_store.acquisition.direct_scrape import DirectScrapeService
from research_store.acquisition.models import DirectScrapeRequest, SearchAdapterResult
from research_store.acquisition.ports import (
    CandidateScrapeAdapter,
    DirectScrapeAdapter,
    SearchAdapter,
)
from research_store.acquisition.service import AcquisitionService
from research_store.config import StoreConfig
from research_store.domain import SearchAdapterResult as DomainSearchAdapterResult
from research_store.ports import SearchAdapter as LegacySearchAdapter

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "scripts" / "research_store"
ACQUISITION = STORE / "acquisition"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _defined_classes(path: Path) -> set[str]:
    return {node.name for node in _tree(path).body if isinstance(node, ast.ClassDef)}


def _top_level_imports(path: Path) -> set[str]:
    imports: set[str] = set()
    for node in _tree(path).body:
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            prefix = "." * node.level
            imports.add(f"{prefix}{node.module}")
    return imports


def test_legacy_acquisition_symbols_are_same_object_compatibility_facades() -> None:
    assert legacy_acquisition_service.AcquisitionService is AcquisitionService
    assert (
        legacy_acquisition_service.FirecrawlSearchAdapter
        is BoundedFirecrawlSearchAdapter
    )
    assert research_store.FirecrawlSearchAdapter is BoundedFirecrawlSearchAdapter
    assert (
        legacy_bounded_acquisition.BoundedFirecrawlSearchAdapter
        is BoundedFirecrawlSearchAdapter
    )
    assert legacy_direct_scrape.DirectScrapeService is DirectScrapeService
    assert legacy_direct_scrape.DirectScrapeRequest is DirectScrapeRequest
    assert (
        legacy_direct_scrape.FirecrawlDirectScrapeAdapter
        is FirecrawlDirectScrapeAdapter
    )
    assert (
        fsearch_service.MetadataOnlyFirecrawlSearchAdapter
        is MetadataOnlyFirecrawlSearchAdapter
    )
    assert SearchAdapterResult is DomainSearchAdapterResult
    assert LegacySearchAdapter is SearchAdapter


def test_acquisition_package_initializer_is_cycle_safe_and_inert() -> None:
    tree = _tree(ACQUISITION / "__init__.py")
    imports = [
        node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert imports == []


def test_root_package_no_longer_rewrites_acquisition_adapter_global() -> None:
    source = (STORE / "__init__.py").read_text(encoding="utf-8")
    assert "_acquisition_service" not in source
    assert "_acquisition_service.FirecrawlSearchAdapter" not in source


def test_application_modules_have_no_top_level_transport_dependency() -> None:
    for relative in ("service.py", "direct_scrape.py"):
        imports = _top_level_imports(ACQUISITION / relative)
        assert "subprocess" not in imports
        assert not any(".adapters" in module for module in imports)


def test_concrete_firecrawl_classes_are_defined_only_in_adapter_package() -> None:
    legacy_paths = (
        STORE / "acquisition_service.py",
        STORE / "bounded_acquisition.py",
        STORE / "direct_scrape_service.py",
        STORE / "fsearch_service.py",
    )
    concrete_names = {
        "BoundedFirecrawlSearchAdapter",
        "FirecrawlDirectScrapeAdapter",
        "MetadataOnlyFirecrawlSearchAdapter",
    }
    for path in legacy_paths:
        assert _defined_classes(path).isdisjoint(concrete_names)

    assert "BoundedFirecrawlSearchAdapter" in _defined_classes(
        ACQUISITION / "adapters" / "bounded_firecrawl.py"
    )
    assert "FirecrawlDirectScrapeAdapter" in _defined_classes(
        ACQUISITION / "adapters" / "firecrawl_scrape.py"
    )
    assert "MetadataOnlyFirecrawlSearchAdapter" in _defined_classes(
        ACQUISITION / "adapters" / "firecrawl_search.py"
    )


def test_search_service_requires_explicit_adapter_before_uow_or_provider_use(
    tmp_path: Path,
) -> None:
    run_id = uuid4()
    uow_calls = 0

    def forbidden_uow() -> Any:
        nonlocal uow_calls
        uow_calls += 1
        raise AssertionError("UoW must not be touched without an adapter")

    context = AuthoritativeAcquisitionContext(
        database_url="postgresql://unused.invalid/review",
        blob_root=tmp_path,
        schema_heads=frozenset({"head"}),
        run_id=run_id,
        run_state="acquiring",
        lifecycle_revision=1,
        dry_run=False,
    )
    service = AcquisitionService(uow_factory=forbidden_uow, search_adapter=None)

    with pytest.raises(RuntimeError, match="explicit SearchAdapter"):
        service.execute_search(
            run_id,
            "bounded acquisition",
            authority_context=context,
        )

    assert uow_calls == 0


def test_direct_scrape_preflight_failure_prevents_adapter_construction() -> None:
    adapter_constructions = 0

    def adapter_factory() -> DirectScrapeAdapter:
        nonlocal adapter_constructions
        adapter_constructions += 1
        raise AssertionError("adapter must not be constructed after preflight failure")

    def fail_preflight(**_kwargs: Any) -> AuthoritativeAcquisitionContext:
        raise RuntimeError("preflight rejected")

    service = DirectScrapeService(
        config=cast(StoreConfig, object()),
        uow_factory=lambda: None,
        blob_store=object(),
        corpus_service=object(),
        adapter_factory=adapter_factory,
        preflight=fail_preflight,
        authority_check=lambda _factory: None,
    )

    with pytest.raises(RuntimeError, match="preflight rejected"):
        service.execute(uuid4(), [DirectScrapeRequest(url="https://example.com")])

    assert adapter_constructions == 0


def test_generic_composition_root_selects_bounded_adapter_explicitly() -> None:
    source = (STORE / "container.py").read_text(encoding="utf-8")
    assert "BoundedFirecrawlSearchAdapter()" in source
    assert "search_adapter=adapter" in source


def test_bounded_extraction_policy_depends_on_candidate_scrape_port() -> None:
    path = STORE / "bounded_orchestrator.py"
    source = path.read_text(encoding="utf-8")
    imports = _top_level_imports(path)
    assert "BoundedFirecrawlSearchAdapter" not in source
    assert CandidateScrapeAdapter.__name__ in source
    assert ".acquisition.ports" in imports
    assert "self.scrape_adapter = scrape_adapter" in source


def test_production_orchestration_selects_bounded_candidate_transport() -> None:
    path = STORE / "orchestration" / "composition.py"
    source = path.read_text(encoding="utf-8")
    assert "class ProductionBoundedExtractionStage" in source
    assert "BoundedFirecrawlSearchAdapter()" in source
    assert "extraction_stage_cls=ProductionBoundedExtractionStage" in source

    resume_source = (STORE / "search_provenance.py").read_text(encoding="utf-8")
    assert "from .acquisition.service import SearchProvenanceError" in resume_source
    assert "ProductionBoundedExtractionStage" in resume_source
    assert "extraction_stage_cls=extraction_stage_cls" in resume_source


def test_direct_scrape_default_selection_is_confined_to_builder_scope() -> None:
    path = ACQUISITION / "direct_scrape.py"
    imports = _top_level_imports(path)
    assert not any(".adapters" in module for module in imports)
    source = path.read_text(encoding="utf-8")
    assert (
        "from .adapters.firecrawl_scrape import FirecrawlDirectScrapeAdapter" in source
    )
    assert source.index("def build_direct_scrape_service") < source.index(
        "from .adapters.firecrawl_scrape import FirecrawlDirectScrapeAdapter"
    )


def test_authority_facade_preserves_historical_module_patch_hooks() -> None:
    assert legacy_acquisition_authority.os is canonical_acquisition_authority.os
    assert (
        legacy_acquisition_authority.tempfile
        is canonical_acquisition_authority.tempfile
    )
