# Acquisition capability boundary

Issue #262 consolidated the acquisition vertical slice while preserving the
existing authoritative research contracts. Phase-5 issue #267 further separates
composition from application implementation; this document reflects the current
post-#267 ownership while preserving the #262 behavioral boundary.

The refactors are structural. They do not broaden provider behavior, change
PostgreSQL authority, replace Firecrawl, or alter bounded extraction,
idempotency, retry, provenance or failure semantics.

## Authority and behavioral invariants

The following contracts remain authoritative:

- PostgreSQL owns research-run lifecycle state, acquisition records,
  idempotency, invocation/provenance identity, candidate identity, and corpus
  persistence state.
- `BLOB_ROOT` owns immutable content-addressed provider/corpus bytes.
- Qdrant is a rebuildable projection rather than an authority store.
- Valkey is optional transient coordination rather than durable workflow state.
- Acquisition fails closed before constructing or invoking Firecrawl when
  database configuration, schema head, privileges, run eligibility, lifecycle
  revision or durable blob-root readiness is invalid.
- Firecrawl search discovery does not perform an implicit scrape.
- Candidate scrape/extraction is a distinct bounded operation with the
  established first-byte, provider-operation and overall-candidate deadlines,
  retry limits, failure classification, diagnostic redaction and cancellation
  semantics.
- Search idempotency/provenance, direct-scrape replay, extraction-attempt
  lineage, and failure persistence remain PostgreSQL-authoritative.

## Boundary map

| Responsibility | Canonical location | Dependency rule |
|---|---|---|
| Acquisition authority/preflight | `research_store.acquisition.authority` | May use PostgreSQL/config/blob readiness; must not construct provider transport. |
| Acquisition-facing models | `research_store.acquisition.models` | Shared by application policy and adapters. `SearchAdapterResult` remains a same-object compatibility exposure of the existing domain model. |
| Provider ports | `research_store.acquisition.ports` | Defines `SearchAdapter`, `CandidateScrapeAdapter`, and `DirectScrapeAdapter`; contains no concrete provider. |
| Search application/persistence policy | `research_store.acquisition.service` | Depends on `SearchAdapter`, never a concrete Firecrawl class. |
| Bounded extraction stage policy | `research_store.bounded_orchestrator.BoundedExtractionStage` | Depends on `CandidateScrapeAdapter`; it never imports or constructs Firecrawl. |
| Direct-scrape application/persistence policy | `research_store.acquisition.direct_scrape_application` | Depends on `DirectScrapeAdapter`; contains authority, persistence, idempotency, retry and failure semantics and does not depend on composition. |
| Historical direct-scrape capability surface | `research_store.acquisition.direct_scrape` | Same-object facade over the application implementation; historical builder delegates to canonical composition. |
| Bounded Firecrawl search/scrape transport | `research_store.acquisition.adapters.bounded_firecrawl` | Concrete network/CLI adapter preserving issue #216 bounded behavior. |
| Metadata-only Firecrawl search transport | `research_store.acquisition.adapters.firecrawl_search` | Concrete discovery-only search adapter used by authoritative `fsearch`. |
| Direct Firecrawl scrape transport | `research_store.acquisition.adapters.firecrawl_scrape` | Concrete direct scrape CLI adapter; contains no persistence policy. |
| Generic acquisition composition | `research_store.composition.build_acquisition_service` | Selects `BoundedFirecrawlSearchAdapter` when no adapter is explicitly supplied; `research_store.container` is a compatibility re-export. |
| Production bounded extraction primitive | `research_store.production_topology.ProductionBoundedExtractionStage` | Injects `BoundedFirecrawlSearchAdapter` through `CandidateScrapeAdapter`; contains no service/UoW/config resolution or workflow policy. |
| Fresh/resumable production orchestration | `research_store.composition` | Selects bounded acquisition plus the production bounded extraction primitive. |
| Authoritative fsearch composition | `research_store.fsearch_policy_service.build_policy_fsearch_service` | Selects metadata-only search transport and the public direct-scrape builder surface. |
| Direct/fscrape composition | `research_store.composition.build_direct_scrape_service` / `research_store.fscrape_service` | Selects `FirecrawlDirectScrapeAdapter` at the composition edge after application authority boundaries are preserved. |

The `research_store.acquisition` package initializer remains intentionally inert.
Callers import explicit submodules, preventing package initialization from
pulling PostgreSQL or provider transport through an implicit cycle.

## Explicit adapter selection

The provider behavior established by #262 is unchanged:

| Call path | Adapter selected |
|---|---|
| Generic `build_acquisition_service()` | `BoundedFirecrawlSearchAdapter` |
| Production bounded candidate extraction | `ProductionBoundedExtractionStage` injects `BoundedFirecrawlSearchAdapter` through `CandidateScrapeAdapter` |
| Authoritative `fsearch` | `MetadataOnlyFirecrawlSearchAdapter` |
| Direct scrape / `fscrape` | `FirecrawlDirectScrapeAdapter` |
| Direct test/application injection | Caller-supplied implementation of the corresponding port |

`AcquisitionService` does not choose a concrete adapter. A directly constructed
service with no adapter rejects provider execution before creating a provider
invocation or touching its UoW.

`BoundedExtractionStage` accepts a `CandidateScrapeAdapter`; it does not construct
Firecrawl. The narrow `production_topology` leaf supplies the concrete bounded
adapter only for production stage construction. The canonical fresh/resumable
builders in `research_store.composition` select that stage explicitly, while
historical direct orchestrator builders use the same leaf primitive to preserve
their Phase-4 runtime defaults without depending back on composition.

`DirectScrapeService` receives an adapter factory. Its execution order remains:

```text
preflight
-> direct-scrape privilege validation
-> persisted invocation/candidate resolution
-> adapter construction
-> provider invocation
```

A preflight or privilege failure therefore cannot construct or invoke Firecrawl.
The #267 physical split of the application module does not change this ordering.

## Direct-scrape Phase-5 split

The initial #267 implementation left `DirectScrapeService` and its historical
builder in the same application module. Because that builder delegated to the
canonical root, the application module depended back on composition.

The reviewed remediation establishes:

```text
acquisition.direct_scrape              compatibility facade + builder
          |
          +--> direct_scrape_application   application/persistence policy
          |
          +--> composition.build_direct_scrape_service
                       |
                       +--> direct_scrape_application.DirectScrapeService
                       +--> FirecrawlDirectScrapeAdapter
```

The service/error classes exposed from `acquisition.direct_scrape` are the exact
objects owned by `direct_scrape_application`; no wrapper subclass or copied
model was introduced. The application implementation contains no composition or
concrete-adapter dependency.

## Compatibility facades and caller audit

Active historical callers are retained as thin compatibility surfaces rather
than duplicate implementations:

| Historical surface | Canonical target / compatibility behavior |
|---|---|
| `research_store.acquisition_authority` | Re-exports `research_store.acquisition.authority`; also exposes the same `os` and `tempfile` module objects for established test/injection hooks. |
| `research_store.acquisition_service` | Re-exports canonical application service/errors; historical `FirecrawlSearchAdapter` aliases `BoundedFirecrawlSearchAdapter`. |
| `research_store.bounded_acquisition` | Re-exports canonical bounded adapter. |
| `research_store.acquisition.direct_scrape` | Re-exports the direct-scrape application implementation and retains the capability-local builder as a composition delegate. |
| `research_store.direct_scrape_service` | Re-exports the direct-scrape capability/models plus concrete scrape adapter for historical callers. |
| `research_store.ports.SearchAdapter` | Same object as `research_store.acquisition.ports.SearchAdapter`. |
| `research_store.FirecrawlSearchAdapter` | Root compatibility alias to `BoundedFirecrawlSearchAdapter`; no module-global rewrite occurs. |
| `research_store.fsearch_service.MetadataOnlyFirecrawlSearchAdapter` | Compatibility import of the canonical metadata-only adapter. |

Future removal of these surfaces requires a fresh caller/reference audit and is
owned by later Phase-5 compatibility cleanup, not by #267.

## #262 findings and current disposition

The original acquisition refactor corrected these structural defects, and #267
does not reopen them:

1. hidden import-time adapter replacement was removed;
2. concrete Firecrawl search implementation was removed from application
   service code;
3. direct-scrape provider mechanics were separated from application/persistence
   policy;
4. metadata-only fsearch transport moved behind the adapter package;
5. nested acquisition packages were registered and covered by wheel/isolation
   tests;
6. authority relocation preserved the Alembic root/head contract;
7. the acquisition package initializer remained inert and cycle-safe;
8. bounded extraction policy depends on `CandidateScrapeAdapter`, not concrete
   Firecrawl transport.

Phase-5 #267 strengthens item 3 further by separating the direct-scrape
application implementation from the historical builder facade. It also moves
shared production bounded-stage adapter injection to the constrained
`production_topology` leaf so historical orchestrator builders do not depend on
a composition facade.

## Test and package evidence

The current regressions cover:

- same-object acquisition/direct-scrape compatibility identities;
- inert acquisition package initialization;
- no application-level concrete adapter dependency;
- preflight-before-adapter construction;
- bounded extraction port ownership and production adapter injection;
- canonical direct-scrape application independence from composition;
- historical builder delegation to canonical composition;
- checkpoint/smart historical builder use of the leaf production topology;
- wheel inclusion and isolated import of both
  `acquisition.direct_scrape_application` and `production_topology`.

Existing acquisition-authority, bounded-preflight, acquisition-service,
direct-scrape, authoritative-fsearch/fscrape, PostgreSQL acquisition repository,
package-boundary and orchestration suites remain the behavioral authorities.
Structural AST checks do not replace these runtime tests.

## #262 acceptance-criteria mapping after #267

| #262 criterion | Current implementation | Required evidence |
|---|---|---|
| Coherent acquisition package | `acquisition/{authority,models,ports,service,direct_scrape_application}.py` plus compatibility `direct_scrape.py` | issue-specific structural tests; package/wheel test |
| Explicit Firecrawl adapter boundary | `acquisition/adapters/*`; provider ports | structural adapter-ownership and focused adapter/service tests |
| Preserve authority/direct/search/scrape contracts | unchanged authority/application semantics and same-object facades | authority/service/fsearch/fscrape regressions and integration tests |
| Preserve bounded acquisition/failure semantics | bounded adapter + shared provider preflight + port-driven bounded stage | issue #216 and acquisition/direct-scrape failure tests |
| Application policy does not select concrete transport | application services receive ports/factories; canonical/leaf production wiring selects adapters | #262/#267 import and side-effect tests plus production composition tests |
| Remove duplicate wrappers only after audit | active historical paths remain thin facades | caller/reference evidence and compatibility identity tests |

## Validation handoff

The exact-head local validation contract for the combined #262/#267 boundary is
defined in `references/composition-root.md` and
`references/local-agent-validation.md`. Any PostgreSQL reset-authorized or
Qdrant-mutating test must use `scripts/disposable-test-services`; persistent
personal services are never review targets.

No migration, schema, transaction or authority change is introduced by the
Phase-5 module split.
