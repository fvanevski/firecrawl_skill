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
authority**. The invariants that bound acquisition are:

- **Provider recency is discovery-only.** A bounded discovery `tbs` window
  narrows *which documents are fetched*. It never qualifies a passage. Exact
  local discovery semantics are persisted independently of the `ResearchSpec`;
  a provider projection may be a coarser non-narrowing superset (`qdr:5d` may
  project to `qdr:w`) or may be left unbounded when no safe provider bound
  exists. Neither case weakens local evidence qualification.
- **Candidate admission uses the persisted search-response clock.** Deterministic
  candidate temporal assessment is evaluated against the exact persisted
  `search_response.responded_at`, never the replay wall clock. Replaying one
  idempotent provider response therefore reproduces the same temporal admission
  payload.
- **Publication and update provenance are distinct.** Explicit
  `datePublished`/publication metadata may establish publication authority.
  Explicit `dateModified`, update metadata, bounded page-visible `Updated`/`Last
  updated` markers, and HTTP `Last-Modified` are retained as update/modification
  signals, never publication signals. `Last-Modified` remains identified as an
  HTTP-header signal rather than being upgraded to stronger page metadata.
- **Cross-source disagreement fails closed.** Candidate, request, and document
  publication/update observations are reconciled without precedence guessing.
  Distinct explicit values, an explicit invalid signal, or a previously known
  explicit conflict produce unknown authority plus conflict/invalid provenance;
  a later valid signal does not erase the conflict. Multiple identical explicit
  observations may be recorded as consistent authority.
- **Live-blog/post temporal evidence remains granular.** Nested JSON-LD entries
  are traversed under hard bounds and their per-entry publication/update
  provenance is retained in `structured_temporal_segments`. Conflicting post
  timestamps are not collapsed into a fabricated page-level timestamp. The
  existing corpus/chunk model does not invent a chunk timestamp when the source
  does not expose a deterministic chunk-to-post binding.
- **Generic dates remain hints only.** Provider-generic `date`, URL-embedded
  dates, retrieval time, and discovery recency never become publication or
  update authority merely because they parse as dates.
- **Semantic basis controls pre-scrape admission.** An old publication plus a
  recent authoritative update can satisfy a freshness-oriented candidate check,
  while that same candidate remains ineligible for a strict publication window.
  Known ineligible candidates are skipped before scrape budget is consumed;
  unknown candidates may remain bounded investigative candidates but cannot
  satisfy required temporal coverage until authority exists.
- **LLM triage cannot override deterministic ineligibility.** Candidate cards may
  expose bounded deterministic temporal assessment for relevance/source
  suitability reasoning. A model decision cannot resurrect an `ineligible`
  candidate.
- **No automatic scope relaxation.** When authoritative passages cannot satisfy
  a persisted `ResearchSpec`, coverage diagnostics classify the gap and never
  mutate the spec. `automatic_scope_relaxation` is always `False`; relaxing the
  temporal scope requires a persisted `ResearchSpec` revision, not an evidence
  waiver or in-run widening.

Canonical owners are `research_store.temporal_candidate` (bounded explicit
signal extraction and normalization), `research_store.temporal_corpus`
(cross-source reconciliation and ingestion provenance),
`research_store.candidate_temporal_policy` (ResearchSpec-based candidate
assessment), `research_store.temporal_coverage` (typed gap diagnostics), and
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
ledger, and `references/audit-remediation-307.md` for the temporal smart-search
acceptance map.
