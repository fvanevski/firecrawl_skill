# Research-store composition root

This document describes the **final** composition topology after issue #269. Migration-era composition aliases from #267 are no longer part of the supported internal architecture.

## Governing dependency rule

`firecrawl_skill.research_store.composition` is the single general dependency-construction root. It may depend broadly on application services, repositories, infrastructure adapters, and the narrow `production_topology` leaf. Application/domain modules must not depend back on `composition` merely to obtain an implementation symbol.

```text
operator / package entry point
        -> research_store.composition
             -> application services / repositories / infrastructure
             -> production_topology leaf primitive

historical orchestrator classes
        -> production_topology leaf primitive
        -> bounded extraction port + concrete provider adapter
```

The following migration aliases are removed by #269 after caller migration:

- `research_store.container`;
- `research_store.orchestration.composition`;
- `research_store.acquisition.direct_scrape` as a facade/builder alias.

Callers import the canonical root directly. No replacement composition facade may be added.

## Responsibility map

| Responsibility | Final owner |
|---|---|
| Canonical UoW factory | `research_store.composition.build_uow_factory` |
| Service/runtime construction | `research_store.composition` |
| Fresh/resumable production orchestration | `research_store.composition` |
| Reusable bounded extraction primitive | `research_store.production_topology.ProductionBoundedExtractionStage` |
| Direct-scrape application/persistence policy | `research_store.acquisition.direct_scrape_application` |
| Direct-scrape provider adapter | `research_store.acquisition.adapters.firecrawl_scrape` |
| Direct-scrape result/request models | `research_store.acquisition.models` |
| Operator UoW helpers | `research_store.store_runtime`, `research_store.index_admin` |

`production_topology` is intentionally narrow: it injects the production bounded adapter into the bounded extraction stage and owns no configuration resolution, UoW construction, persistence, transaction, or workflow policy. It is not a second composition root.

`handoff_admin.build_handoff` remains intentionally distinct because its injectable UoW constructor binds an additional `chunker_name` argument and is therefore not an equivalent instance of the canonical eight-field factory.

## Direct-scrape final topology

The #267 split correctly separated `acquisition.direct_scrape_application` from composition but temporarily retained `acquisition.direct_scrape` as a same-object facade and builder delegate. #269 completes that migration:

```text
composition.build_direct_scrape_service
        -> acquisition.direct_scrape_application.DirectScrapeService
        -> acquisition.adapters.firecrawl_scrape.FirecrawlDirectScrapeAdapter
        -> acquisition.models
```

The application implementation contains no composition back-edge. Internal callers use the application module for service/error types, `acquisition.models` for data contracts, the adapter module for concrete transport, and `composition.build_direct_scrape_service` for construction.

## Retained narrow compatibility contracts

Issue #269 removes package/module migration facades. It does **not** silently remove unrelated campaign contracts that have explicit behavioral authority:

- the root package may expose stable public aliases such as `FirecrawlSearchAdapter` while provider selection remains explicit in composition;
- the issue #217 UoW class-level ingestion-batch contract remains because its tests and persistence semantics are independently authoritative;
- the `PostgresUnitOfWork.persist_ingest` class signature remains the documented narrow compatibility contract while its runtime implementation delegates to the repository-bound owner.

These are explicit API/runtime contracts, not duplicate module implementations or migration-only import paths.

## Codex Review disposition

The current PR #292 exact-head review state exposes **zero review threads** and no Codex-authored review/comment. Earlier #267 documentation recorded that a Codex thread had existed on the old `orchestration/composition.py` import block, but its exact body was never captured. The conservative remediation that addressed that historical suggestion class remains in the final architecture: historical orchestrator builders depend on `production_topology`, not on a composition facade, and the canonical root remains the only general composition owner.

No unverifiable inline wording is treated as an acceptance criterion for PR #292. If immutable Codex comment/review evidence becomes available, it must be evaluated against this final topology without local architectural redesign.

## Final structural authority

`tests/unit/test_issue_267_composition_root.py` must be updated to assert final ownership rather than same-object compatibility with removed aliases. `tests/contract/test_issue_269_final_topology.py` independently rejects the removed composition/facade paths and any surviving import or dynamic patch target to them.

Behavioral acquisition, orchestration, direct-scrape, PostgreSQL, package/wheel, Ruff, and Pyrefly authorities remain cumulative. Exact-head local validation is execution evidence only; it may not change these ownership decisions.
