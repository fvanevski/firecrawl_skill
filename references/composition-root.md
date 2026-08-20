# Research-store composition root

Issue #267 centralizes research-store dependency construction while preserving
the Phase-4 capability boundaries and all runtime, persistence, schema, CLI,
transaction, provenance, and authority contracts inherited from #246 and #260.

This reference is the Phase-5 authority for the composition topology introduced
by #267.  It documents the post-review remediation as well as the intended
boundary for later compatibility cleanup in #269.

## Governing dependency rule

Composition may depend broadly on application and infrastructure. Application
and domain implementation modules must not depend back on the canonical
composition root or a compatibility facade for that root.

The allowed direction is:

```text
operator / compatibility facade
        -> research_store.composition
             -> application services / repositories / infrastructure
             -> production_topology leaf primitive

historical direct orchestrator builders
        -> production_topology leaf primitive
        -> bounded extraction port + concrete provider adapter
```

The forbidden direction is:

```text
application/domain implementation -> composition
application/domain implementation -> orchestration.composition facade
```

Function-local imports do not exempt a dependency from this rule. They may
avoid an import-time crash while still creating the wrong architectural edge.

## Responsibility map

| Responsibility | Canonical location | Contract |
|---|---|---|
| Canonical UoW factory | `research_store.composition.build_uow_factory` | The one eight-field `PostgresUnitOfWork` binding used by normal store composition. It intentionally preserves the historical low-level behavior and does not add `require_database()`. |
| Store/service construction | `research_store.composition` | Resolves `StoreConfig`, validates database requirements at the existing public boundaries, constructs services/adapters, and owns no persistence or workflow policy. |
| Fresh/resumable production orchestration | `research_store.composition` | Selects the bounded acquisition stage and production bounded extraction stage explicitly. |
| Reusable bounded production-stage primitive | `research_store.production_topology` | One leaf class that injects `BoundedFirecrawlSearchAdapter` into `BoundedExtractionStage`; it resolves no `StoreConfig`, UoW, service, transaction, or workflow state. |
| Direct-scrape application/persistence policy | `research_store.acquisition.direct_scrape_application` | Owns authority checks, idempotency, retry, provenance and persistence behavior; it has no dependency on composition. |
| Historical acquisition direct-scrape surface | `research_store.acquisition.direct_scrape` | Thin same-object facade plus the historical builder, which delegates to the canonical root. |
| Historical general builder surface | `research_store.container` | Thin re-export facade over canonical root builders. |
| Historical orchestration composition surface | `research_store.orchestration.composition` | Thin facade: production builder functions re-export the canonical root; `ProductionBoundedExtractionStage` re-exports the leaf primitive. |
| Operator UoW helpers | `research_store.store_runtime`, `research_store.index_admin` | Reuse the canonical UoW factory rather than reconstructing an equivalent partial. |

`handoff_admin.build_handoff` remains intentionally distinct. Its injectable
UoW constructor binds an additional `chunker_name` argument and is therefore not
an equivalent instance of the canonical eight-field factory.

## Independent-review remediation

### Blocking: direct-scrape application depended back on composition

The reviewed head `d8a6fdaf0eb8ce36d95c7bf629b75f608ee280de`
placed the full PostgreSQL-authoritative direct-scrape application implementation
and its historical builder in `acquisition.direct_scrape`. The builder lazily
imported `research_store.composition`, while the root imported
`DirectScrapeService` from that same module. This produced the forbidden lazy
back-edge:

```text
composition -> acquisition.direct_scrape -> composition
```

The remediation separates physical ownership without changing public object
identity or application behavior:

- `acquisition.direct_scrape_application` owns the service implementation and
  persistence policy and does not import either composition surface;
- `acquisition.direct_scrape` is now a true compatibility facade and retains the
  historical builder signature;
- `composition.build_direct_scrape_service` imports and constructs the
  application implementation directly;
- compatibility tests require facade service/error objects to be the exact
  implementation objects;
- import-direction tests require the implementation module to have no
  composition dependency.

No SQL, transaction, authority, idempotency, retry, or provider-ordering logic
was changed by this split.

### Important non-blocking / semantic-locality concern

The remediation needed a reusable production extraction-stage default for
historical `CheckpointResearchOrchestrator.build()` and
`ProvenanceResumableResearchOrchestrator.build()` callers. Duplicating the class
or routing those application/orchestrator modules through the canonical root
would either create duplicate construction or recreate the forbidden dependency.

`research_store.production_topology` is therefore intentionally narrow rather
than a second general composition module. It contains exactly one production
stage subclass and only the concrete adapter injection needed to preserve the
Phase-4 historical builder behavior. Regression coverage rejects `StoreConfig`,
UoW, `CorpusService`, persistence/workflow operations, or composition imports in
that module. General service, UoW and orchestrator construction remains in
`research_store.composition`.

### Codex Review automated suggestion

The focused GitHub review surface exposes one Codex review thread anchored to
the old `scripts/research_store/orchestration/composition.py` import block. The
focused connector does not expose the inline comment body, so its exact wording
is **UNVERIFIED** here and is not fabricated.

The source path under review has nevertheless been remediated conservatively:

- `CheckpointResearchOrchestrator.build()` no longer imports
  `orchestration.composition`;
- `ProvenanceResumableResearchOrchestrator.build()` no longer imports
  `orchestration.composition`;
- both historical builders obtain the same bounded production extraction class
  from the leaf `production_topology` primitive;
- `orchestration.composition` is only a compatibility facade and no longer owns
  or supplies a root-defined stage class back to those builders;
- the canonical root still owns fresh/resumable production-orchestrator
  construction.

The local review handoff must read the exact Codex inline body with native
authenticated `gh` and report whether this source change semantically satisfies
it. That is a verification step only: the local agent must not redesign or
modify production code in response. Any non-equivalent demand returns to
Central ChatGPT.

### Test/documentation gap: “wiring only” was under-specified

The original #267 test only rejected a few SQL/transaction spellings and
required top-level functions to start with `build_`. That did not adequately
protect against moving an existing workflow/persistence operation into the root.

The strengthened regression now additionally:

- detects imports of both the canonical root and historical orchestration
  composition surface across the package;
- allows those imports only in explicit compatibility/operator wiring modules;
- proves the canonical direct-scrape application has no composition back-edge;
- proves historical checkpoint/smart builders depend on `production_topology`,
  not either composition surface;
- rejects representative persistence/workflow operation calls such as
  `execute`, `execute_search`, `run`, `begin`, `complete`, `prepare_ingest`,
  `persist_ingest`, policy evaluation/recording, transaction calls and state
  transitions from `composition.py`;
- requires `composition.py` to own no class implementation bodies;
- constrains `production_topology.py` to one leaf wiring class with no policy or
  persistence operations.

These structural tests complement, rather than replace, behavioral pytest,
Pyrefly, Ruff and service-backed integration authorities.

### Package-boundary gap

The application split and leaf topology add two package modules. The existing
wheel/isolation regression now explicitly requires both
`acquisition/direct_scrape_application.py` and `production_topology.py` in the
built wheel, imports them from an isolated installation, and proves the
historical direct-scrape facade exposes the exact application service object.

## Acceptance-criteria mapping

| #267 criterion | Implementation | Regression/evidence |
|---|---|---|
| One canonical UoW/repository factory | `composition.build_uow_factory` | exact `partial` target/argument contract and package-wide duplicate scan |
| Eliminate repeated equivalent construction | `container`, `store_runtime`, `index_admin`, direct-scrape composition delegate/re-export canonical factory | AST duplicate-construction scan and same-object factory tests |
| Explicit composition root | `research_store.composition` | builder identities, production orchestrator wiring and import-topology checks |
| Preserve public builder behavior | `container`, `orchestration.composition`, `acquisition.direct_scrape` facades | same-object and delegation tests; existing CLI/capability suites |
| Composition owns wiring, not business policy | canonical root plus narrow `production_topology` leaf | forbidden operation/import scans plus existing runtime authority suites |
| Builder/composition equivalence tests | `tests/unit/test_issue_267_composition_root.py`, acquisition/orchestration/package regressions | CI registration plus local exact-head validation |

## Validation authority and local handoff

Central source changes are not a substitute for exact-head host validation. The
local OpenCode agent must act only as the execution/evidence layer.

Use the following contract on the exact PR head:

1. **Native Git** — `git fetch origin`; verify the requested 40-character head,
   base SHA and complete changed-file list. Use raw/native Git for these exact
   identities and decisive diffs.
2. **Serena** — first-line semantic inspection of the changed symbols,
   declarations/references and import topology; use no memories; audit after any
   centrally prescribed mechanical repair. No substantive redesign.
3. **RTK** — routine successful Ruff, Pyrefly, pytest and search output where
   filtering preserves evidence. Use raw/native output for failures and exact
   Git/runtime evidence.
4. **OpenViking** — only bounded historical rationale if needed; never source,
   Git, CI, database or runtime authority.
5. Run changed-scope `ruff check` and `ruff format --check --diff` on exact ACMR
   Python paths.
6. Run repo-pinned `pyrefly check <changed.py ...>` on the same exact ACMR Python set, including changed tests, followed later by full-project `pyrefly check` with no file arguments.
7. Run the smallest focused regressions first, including
   `test_issue_267_composition_root.py`, `test_issue_262_acquisition_slice.py`,
   `test_corpus_service_refactor.py`, `test_orchestration_package.py` and
   `test_package_boundary.py`, then applicable direct-scrape/orchestrator
   capability tests and broader CI-gated suites.
8. Any PostgreSQL reset-authorized or Qdrant-mutating validation must use
   `scripts/disposable-test-services` exactly as required by the review
   contract. Persistent personal services are never test targets.
9. Read the exact Codex inline review body with native authenticated `gh` and
   report its text/disposition; do not make substantive code changes locally.
10. Report every authority separately. A failure is evidence, not permission to
    weaken tests, Pyrefly configuration/baseline, or production behavior.

No merge, issue closure or Phase-5 gate closure is implied by this document.
