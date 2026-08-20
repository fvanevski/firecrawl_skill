# Acquisition capability boundary

This document describes the **final** acquisition topology after issue #269. The #262/#267 compatibility surfaces were migration aids; internal callers now target the canonical acquisition package and composition root directly.

## Authority invariants

- PostgreSQL owns research-run lifecycle, acquisition records, idempotency, invocation/provenance identity, candidate identity, and corpus persistence state.
- `BLOB_ROOT` owns immutable content-addressed provider/corpus bytes.
- Qdrant is rebuildable projection state; Valkey is transient coordination only.
- Acquisition fails closed before Firecrawl construction/invocation when authority/preflight requirements fail.
- Firecrawl search discovery never implies scrape.
- Search idempotency/provenance, direct-scrape replay, extraction-attempt lineage, bounded retries/deadlines, failure classification, redaction, and cancellation semantics remain unchanged by compatibility cleanup.

## Final ownership map

| Responsibility | Canonical owner |
|---|---|
| Acquisition authority/preflight | `research_store.acquisition.authority` |
| Acquisition-facing models | `research_store.acquisition.models` |
| Provider ports | `research_store.acquisition.ports` |
| Search application/persistence policy | `research_store.acquisition.service` |
| Candidate ranking | `research_store.acquisition.candidate_ranking` |
| URL/profile classification | `research_store.acquisition.classifier` |
| Bounded Firecrawl search/scrape adapter | `research_store.acquisition.adapters.bounded_firecrawl` |
| Metadata-only Firecrawl search adapter | `research_store.acquisition.adapters.firecrawl_search` |
| Direct Firecrawl scrape adapter | `research_store.acquisition.adapters.firecrawl_scrape` |
| Direct-scrape application/persistence policy | `research_store.acquisition.direct_scrape_application` |
| General/direct-scrape composition | `research_store.composition` |
| Production bounded extraction primitive | `research_store.production_topology` |

`research_store.ports` is retained because it owns substantial cross-capability repository/UoW protocols; its re-export of `SearchAdapter` does not make the module a compatibility facade.

## Removed migration surfaces

After current-source caller migration, #269 removes:

- `research_store.acquisition_authority`;
- `research_store.acquisition_service`;
- `research_store.bounded_acquisition`;
- `research_store.direct_scrape_service`;
- `research_store.acquisition.direct_scrape`.

These modules contain no unique final implementation. Tests that patched them migrate to the exact canonical implementation module rather than preserving an alias for test convenience.

The root package may continue to expose intentionally supported public symbols such as `AcquisitionService`, `FirecrawlSearchAdapter`, direct-scrape result/service symbols, and `build_direct_scrape_service`; those exports now bind directly to canonical owners and do not require legacy modules to exist.

## Explicit adapter selection

| Call path | Adapter selected |
|---|---|
| `composition.build_acquisition_service()` | `BoundedFirecrawlSearchAdapter` |
| Production bounded candidate extraction | `ProductionBoundedExtractionStage` injects `BoundedFirecrawlSearchAdapter` |
| Authoritative `fsearch` | `MetadataOnlyFirecrawlSearchAdapter` |
| Direct scrape / `fscrape` | `FirecrawlDirectScrapeAdapter` |
| Direct test/application injection | caller-supplied implementation of the corresponding port |

Application services receive ports/factories; they do not select concrete provider transports. Direct-scrape execution preserves the authority ordering:

```text
preflight
-> direct-scrape privilege validation
-> persisted invocation/candidate resolution
-> adapter construction
-> provider invocation
```

## Final tests

Final structural coverage must prove:

- canonical package ownership rather than identity with migration facades;
- no application-level concrete-adapter dependency;
- preflight-before-adapter construction;
- bounded extraction port ownership and production adapter injection;
- direct-scrape application independence from composition;
- absence of the removed acquisition facades;
- isolated-wheel inclusion/import of canonical acquisition modules;
- no dynamic patch target references a removed facade.

Existing authority, acquisition-service, fsearch/fscrape, direct-scrape, PostgreSQL repository, package-boundary and orchestration suites remain behavioral authorities after their imports are migrated.
