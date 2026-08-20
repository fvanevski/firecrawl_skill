# Acquisition capability boundary

This document describes the **final post-#269 topology**. Migration facades are
not part of the supported package architecture; Git history preserves their
former compatibility rationale.

## Authority

- PostgreSQL owns run lifecycle, acquisition records, idempotency, provenance,
  candidate identity, and corpus persistence state.
- `BLOB_ROOT` owns immutable content-addressed provider/corpus bytes.
- Qdrant is a rebuildable projection and Valkey is transient coordination.
- Acquisition fails closed before provider construction when database/schema,
  privilege, run-state, revision, or blob-root authority is invalid.
- Search discovery never performs an implicit scrape. Candidate extraction is a
  distinct bounded operation.

## Canonical owners

| Responsibility | Final owner |
|---|---|
| Acquisition authority/preflight | `research_store.acquisition.authority` |
| Models | `research_store.acquisition.models` |
| Provider ports | `research_store.acquisition.ports` |
| Search application/persistence policy | `research_store.acquisition.service` |
| Candidate ranking/budget policy | `research_store.acquisition.candidate_ranking` |
| Target classification | `research_store.acquisition.classifier` |
| Direct-scrape application policy | `research_store.acquisition.direct_scrape_application` |
| Bounded Firecrawl search/scrape adapter | `research_store.acquisition.adapters.bounded_firecrawl` |
| Metadata-only search adapter | `research_store.acquisition.adapters.firecrawl_search` |
| Direct Firecrawl scrape adapter | `research_store.acquisition.adapters.firecrawl_scrape` |
| Production service construction | `research_store.composition` |
| Production bounded extraction leaf | `research_store.production_topology` |

The acquisition package initializer is intentionally inert. Application modules
consume ports and do not import concrete adapters. Concrete provider selection
occurs at `composition` or the narrowly constrained `production_topology` leaf.

## Removed compatibility identities

Final topology contains no flat `acquisition_authority.py`,
`acquisition_service.py`, `bounded_acquisition.py`, or `direct_scrape_service.py`,
and no `acquisition.direct_scrape` builder facade. Repository callers must import
the canonical owner above. A repository-internal historical import is not a
reason to recreate a facade.

## Behavioral invariants

Direct scrape ordering remains:

```text
preflight
-> privilege validation
-> persisted invocation/candidate resolution
-> adapter construction
-> provider invocation
```

`AcquisitionService` cannot choose a concrete provider when directly
constructed. `BoundedExtractionStage` consumes `CandidateScrapeAdapter`.
`DirectScrapeService` consumes an injected adapter factory. Existing authority,
idempotency, retry, bounded-timeout, redaction, cancellation, persistence, and
provenance semantics remain behavioral test authority.

## Validation

Final acceptance requires:

1. physical absence of the migration facades;
2. AST/reference census showing no source/test/operator import or dynamic patch
   target points at them;
3. focused acquisition authority/service/fsearch/fscrape tests;
4. package/wheel isolation without `scripts/` runtime modules;
5. changed-scope and full-project Pyrefly with no baseline/config weakening;
6. disposable PostgreSQL for reset/mutation tests.

See `references/issue-269-final-cleanup.md` for the exact handoff and deletion
ledger.
