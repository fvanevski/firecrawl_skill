"""Final acquisition vertical-slice boundary regressions.

Issue #269 removes the migration-era flat acquisition facades.  These tests
therefore exercise canonical capability owners directly and treat the old paths
as files that must be physically absent in the final tree.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.acquisition.adapters.bounded_firecrawl import (
    BoundedFirecrawlSearchAdapter,
)
from firecrawl_skill.research_store.acquisition.adapters.firecrawl_scrape import (
    FirecrawlDirectScrapeAdapter,
)
from firecrawl_skill.research_store.acquisition.adapters.firecrawl_search import (
    MetadataOnlyFirecrawlSearchAdapter,
)
from firecrawl_skill.research_store.acquisition.authority import (
    AuthoritativeAcquisitionContext,
)
from firecrawl_skill.research_store.acquisition.direct_scrape_application import (
    DirectScrapeService,
)
from firecrawl_skill.research_store.acquisition.models import DirectScrapeRequest
from firecrawl_skill.research_store.acquisition.ports import (
    CandidateScrapeAdapter,
    DirectScrapeAdapter,
)
from firecrawl_skill.research_store.acquisition.service import AcquisitionService
from firecrawl_skill.research_store.config import StoreConfig

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "src" / "firecrawl_skill" / "research_store"
ACQUISITION = STORE / "acquisition"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(path: Path) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            result.add("." * node.level + (node.module or ""))
    return result


def test_acquisition_package_initializer_is_inert() -> None:
    tree = _tree(ACQUISITION / "__init__.py")
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert imports == []


def test_flat_acquisition_facades_are_absent() -> None:
    obsolete = (
        STORE / "acquisition_authority.py",
        STORE / "acquisition_service.py",
        STORE / "bounded_acquisition.py",
        STORE / "direct_scrape_service.py",
        ACQUISITION / "direct_scrape.py",
    )
    assert [path.name for path in obsolete if path.exists()] == []


def test_application_modules_do_not_select_concrete_transport() -> None:
    for relative in ("service.py", "direct_scrape_application.py"):
        path = ACQUISITION / relative
        source = path.read_text(encoding="utf-8")
        imports = _imports(path)
        assert "subprocess" not in imports
        assert not any(".adapters" in module for module in imports)
        assert "FirecrawlDirectScrapeAdapter" not in source
        assert "BoundedFirecrawlSearchAdapter" not in source
        assert "MetadataOnlyFirecrawlSearchAdapter" not in source


def test_concrete_provider_classes_live_only_in_adapter_package() -> None:
    assert "class BoundedFirecrawlSearchAdapter" in (
        ACQUISITION / "adapters" / "bounded_firecrawl.py"
    ).read_text(encoding="utf-8")
    assert "class FirecrawlDirectScrapeAdapter" in (
        ACQUISITION / "adapters" / "firecrawl_scrape.py"
    ).read_text(encoding="utf-8")
    assert "class MetadataOnlyFirecrawlSearchAdapter" in (
        ACQUISITION / "adapters" / "firecrawl_search.py"
    ).read_text(encoding="utf-8")


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
        service.execute_search(run_id, "bounded acquisition", authority_context=context)

    assert uow_calls == 0


def test_direct_scrape_preflight_failure_prevents_adapter_construction() -> None:
    adapter_constructions = 0

    def adapter_factory() -> DirectScrapeAdapter:
        nonlocal adapter_constructions
        adapter_constructions += 1
        raise AssertionError("adapter constructed after failed preflight")

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


def test_composition_root_selects_provider_adapters_explicitly() -> None:
    source = (STORE / "composition.py").read_text(encoding="utf-8")
    assert "BoundedFirecrawlSearchAdapter()" in source
    assert "search_adapter=adapter" in source
    assert "from .acquisition.adapters.firecrawl_scrape import FirecrawlDirectScrapeAdapter" in source
    assert "adapter_factory = FirecrawlDirectScrapeAdapter" in source


def test_bounded_extraction_depends_on_candidate_scrape_port() -> None:
    source = (STORE / "bounded_orchestrator.py").read_text(encoding="utf-8")
    assert CandidateScrapeAdapter.__name__ in source
    assert "BoundedFirecrawlSearchAdapter" not in source
    assert "self.scrape_adapter = scrape_adapter" in source


def test_production_topology_is_the_only_default_extraction_adapter_leaf() -> None:
    topology = (STORE / "production_topology.py").read_text(encoding="utf-8")
    composition = (STORE / "composition.py").read_text(encoding="utf-8")
    assert "class ProductionBoundedExtractionStage" in topology
    assert "BoundedFirecrawlSearchAdapter()" in topology
    assert "extraction_stage_cls=ProductionBoundedExtractionStage" in composition
    assert FirecrawlDirectScrapeAdapter.__module__.endswith(
        "acquisition.adapters.firecrawl_scrape"
    )
    assert MetadataOnlyFirecrawlSearchAdapter.__module__.endswith(
        "acquisition.adapters.firecrawl_search"
    )
