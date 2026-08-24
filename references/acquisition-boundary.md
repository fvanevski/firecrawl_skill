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

## Temporal candidate authority

Issue-307 remediation separates **temporal discovery** from **temporal evidence
authority**. The invariants that bound acquisition:

- **Provider recency is discovery-only.** A provider `dateModified`/recency
  signal, and any bounded discovery `tbs` window, narrow *which documents are
  fetched*. They never qualify a passage: discovery time is a non-narrowing
  superset of the evidence window, is persisted as an independent discovery plan
  distinct from the `ResearchSpec`, and is never conflated with evidence time.
- **Publication and update provenance are distinct.** `datePublished`
  (publication authority) and `dateModified` (update/freshness authority) are
  extracted and tracked separately in the temporal corpus and classified
  separately by coverage diagnostics. A generic or provider date is never
  inferred into either authority.
- **No automatic scope relaxation.** When authoritative passages cannot satisfy
  a persisted `ResearchSpec`, coverage diagnostics classify the gap
  (`missing_publication_authority`, `stale_freshness_authority`, …) and never
  mutate the spec. `automatic_scope_relaxation` is always `False`; relaxing the
  scope requires a persisted `ResearchSpec` revision, not an in-run widening.

Canonical owners: `research_store.temporal_candidate` (signal extraction),
`research_store.candidate_temporal_policy` (publication/update window
assessment), `research_store.temporal_coverage` (gap classification), and
`research_store.plan_recency` (discovery-only recency planning).

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
