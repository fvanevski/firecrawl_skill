# Acquisition capability boundary

Issue #262 consolidates the acquisition vertical slice while preserving the
existing authoritative research contracts. The refactor is structural: it does
not broaden provider behavior, change PostgreSQL authority, replace Firecrawl,
or alter the deterministic bounded-extraction policies established by earlier
issues.

## Authority and behavioral invariants

The following contracts are unchanged and remain authoritative:

- PostgreSQL owns research-run lifecycle state, acquisition records,
  idempotency, invocation/provenance identity, candidate identity, and corpus
  persistence state.
- `BLOB_ROOT` owns immutable content-addressed provider/corpus bytes.
- Qdrant is a rebuildable projection rather than an authority store.
- Valkey is optional transient coordination rather than durable workflow state.
- Acquisition must fail closed before constructing or invoking Firecrawl when
  database configuration, schema head, privileges, run eligibility,
  lifecycle revision, or durable blob-root readiness is invalid.
- Firecrawl search discovery does not perform an implicit scrape.
- Candidate scrape/extraction remains a distinct bounded operation with the
  established first-byte, provider-operation, and overall-candidate deadlines,
  retry limits, failure classification, diagnostic redaction, and cancellation
  semantics.
- Search idempotency/provenance, direct-scrape replay, extraction-attempt
  lineage, and failure persistence remain PostgreSQL-authoritative.

## Boundary map

| Responsibility | Canonical location | Dependency rule |
|---|---|---|
| Acquisition authority/preflight | `research_store.acquisition.authority` | May use PostgreSQL/config/blob readiness; must not construct provider transport. |
| Acquisition-facing models | `research_store.acquisition.models` | Shared by application policy and adapters. `SearchAdapterResult` is a same-object compatibility re-export of the existing domain model in this phase. |
| Provider ports | `research_store.acquisition.ports` | Defines `SearchAdapter` and `DirectScrapeAdapter`; contains no concrete provider. |
| Search application/persistence policy | `research_store.acquisition.service` | Depends on `SearchAdapter`, never a concrete Firecrawl class. |
| Direct-scrape application/persistence policy | `research_store.acquisition.direct_scrape` | Depends on `DirectScrapeAdapter`; provider selection is confined to builder/composition scope after authority checks. |
| Bounded Firecrawl search/scrape transport | `research_store.acquisition.adapters.bounded_firecrawl` | Concrete network/CLI adapter; preserves issue #216 bounded behavior. |
| Metadata-only Firecrawl search transport | `research_store.acquisition.adapters.firecrawl_search` | Concrete discovery-only search adapter used by authoritative `fsearch`. |
| Direct Firecrawl scrape transport | `research_store.acquisition.adapters.firecrawl_scrape` | Concrete direct scrape CLI adapter; contains no persistence policy. |
| Generic acquisition composition | `research_store.container.build_acquisition_service` | Selects `BoundedFirecrawlSearchAdapter` when no adapter is explicitly supplied. |
| Authoritative fsearch composition | `research_store.fsearch_policy_service.build_policy_fsearch_service` | Selects metadata-only search transport and direct-scrape service. |
| Direct/fscrape composition | `build_direct_scrape_service` / `research_store.fscrape_service` | Selects `FirecrawlDirectScrapeAdapter` at the composition edge. |

The `research_store.acquisition` package initializer is intentionally inert.
Callers import the explicit submodule they need. This prevents package import
from pulling PostgreSQL or provider transport dependencies through an implicit
cycle and makes dependency direction visible at each call site.

## Explicit adapter selection

The effective provider behavior that existed before #262 is preserved
explicitly instead of through package-global mutation:

| Call path | Adapter selected |
|---|---|
| Generic `build_acquisition_service()` | `BoundedFirecrawlSearchAdapter` |
| Authoritative `fsearch` | `MetadataOnlyFirecrawlSearchAdapter` |
| Direct scrape / `fscrape` | `FirecrawlDirectScrapeAdapter` |
| Direct test/application injection | Caller-supplied implementation of the corresponding port |

`AcquisitionService` itself no longer chooses a concrete adapter. A directly
constructed service with no adapter rejects a valid provider execution before
creating a provider invocation or touching its UoW. This is a configuration
failure, not a transport fallback.

`DirectScrapeService` receives an adapter factory. Its execution order remains
preflight -> direct-scrape privilege validation -> persisted invocation/candidate
resolution -> adapter construction -> provider invocation. A preflight failure
therefore cannot construct or invoke Firecrawl.

## Compatibility facades and caller audit

The pre-refactor paths had active repository callers, especially tests and
entrypoint/composition code. They are retained as thin same-object compatibility
facades rather than duplicate implementations:

| Historical surface | Canonical target / compatibility behavior |
|---|---|
| `research_store.acquisition_authority` | Re-exports `research_store.acquisition.authority`. |
| `research_store.acquisition_service` | Re-exports canonical application service/errors; historical `FirecrawlSearchAdapter` explicitly aliases `BoundedFirecrawlSearchAdapter`. |
| `research_store.bounded_acquisition` | Re-exports canonical bounded adapter. |
| `research_store.direct_scrape_service` | Re-exports canonical direct-scrape application/models plus the concrete scrape adapter for compatibility/composition callers. |
| `research_store.ports.SearchAdapter` | Same object as `research_store.acquisition.ports.SearchAdapter`. |
| `research_store.FirecrawlSearchAdapter` | Explicit root alias to `BoundedFirecrawlSearchAdapter`; it no longer rewrites another module global. |
| `research_store.fsearch_service.MetadataOnlyFirecrawlSearchAdapter` | Compatibility import of the canonical metadata-only adapter while internal composition migrates to the adapter package. |

The obsolete concrete class bodies in the historical acquisition/direct-scrape
modules were removed only after a repository reference audit. Future removal of
these compatibility facades requires a new caller/reference audit and is not
part of #262.

### `SearchAdapterResult` ownership note

`SearchAdapterResult` predates the acquisition package and is used broadly via
`research_store.domain`. #262 exposes that exact class through
`research_store.acquisition.models` rather than creating a second model type.
This preserves object identity and avoids unrelated domain churn. Acquisition
application/adapters import it through the acquisition-facing model surface;
a later domain decomposition may move physical ownership only with a separate
caller audit.

## Findings remediation

### Blocking findings

1. **Hidden import-time adapter replacement.** The root package previously
   assigned `acquisition_service.FirecrawlSearchAdapter =
   BoundedFirecrawlSearchAdapter`. The mutation is removed. Compatibility is a
   normal alias and production selection is explicit in composition.
2. **Duplicate/obsolete Firecrawl search implementation in application
   service.** The concrete subprocess implementation was removed from the
   service module. `AcquisitionService` depends only on `SearchAdapter`.
3. **Direct-scrape application and transport were co-located.** Persistence,
   authority, idempotency, and retry policy now live in
   `acquisition.direct_scrape`; Firecrawl CLI mechanics live in
   `acquisition.adapters.firecrawl_scrape`.
4. **Authoritative fsearch carried another concrete search transport.** The
   metadata-only adapter now lives in `acquisition.adapters.firecrawl_search`;
   `fsearch_service` contains workflow/CLI behavior rather than an adapter
   implementation.
5. **Nested packages could be absent from built wheels.** Both acquisition
   packages are explicitly registered in `pyproject.toml`, and wheel/isolation
   regressions require every canonical acquisition module.
6. **Moving acquisition authority changed its relative repository depth.** The
   canonical authority resolves the Alembic repository root at the corrected
   package depth while retaining the same schema-head contract.
7. **Potential acquisition-package import cycle.** The package initializer is
   intentionally import-free; explicit submodule imports prevent an
   `acquisition -> authority/postgres -> ports -> acquisition` initialization
   cycle.

### Important but non-blocking findings

- Historical imports remain same-object facades instead of wrapper subclasses
  or copied models.
- The old global `SearchAdapter` protocol is not duplicated; it re-exports the
  acquisition-owned port.
- `SearchAdapterResult` is intentionally a same-object re-export rather than a
  new nominal type.
- Existing operator entrypoint targets remain unchanged.
- Concrete adapter aliases retained for compatibility are not provider
  selection inside application service constructors.
- `provider_preflight`, candidate policy/ranking, extraction policy,
  PostgreSQL repositories/schema, Qdrant, and Valkey were deliberately left
  outside this structural issue to avoid behavioral scope expansion.

### Codex Review automated suggestions

No pull request existed for `refactor/acquisition-slice` when this central
implementation was prepared, so there was no repository-specific Codex Review
thread that could be read or truthfully marked resolved before local handoff.
The implementation nevertheless adds explicit regressions for the classes of
structural problem an automated review can detect here: hidden import mutation,
package initialization cycles, duplicate concrete adapters, application-level
transport coupling, compatibility identity drift, fail-closed adapter
construction, and missing wheel contents.

After local validation and PR creation, the central reviewer must fetch the
actual exact-head Codex Review comments. Any concrete suggestion must be
resolved or explicitly dispositioned against the authoritative contracts before
the PR can satisfy the #260 phase gate. This document must not be used as a
claim that future automated comments are already resolved.

### Test and documentary gaps closed by #262

- `scripts/test_issue_262_acquisition_slice.py` covers compatibility identity,
  inert package initialization, removal of the root monkeypatch, concrete
  adapter definition ownership, explicit search-adapter configuration,
  direct-scrape preflight-before-adapter construction, and composition wiring.
- `scripts/test_package_boundary.py` requires all acquisition package/adapters
  in the built wheel and imports the canonical nested package from an isolated
  installation rather than the repository source tree.
- Existing acquisition-authority, bounded-preflight, acquisition-service,
  direct-scrape, authoritative-fsearch/fscrape, PostgreSQL acquisition
  repository, and package-boundary suites remain the behavioral regression
  authority.
- This document supplies the boundary map, compatibility rationale, preserved
  invariants, findings disposition, and acceptance-criteria mapping required by
  the issue.

## Acceptance-criteria mapping

| #262 acceptance criterion | Implementation | Required evidence |
|---|---|---|
| 1. Coherent acquisition package | `acquisition/{authority,models,ports,service,direct_scrape}.py` | issue-specific structural tests; package/wheel test |
| 2. Explicit Firecrawl adapter boundary | `acquisition/adapters/*`; `SearchAdapter` / `DirectScrapeAdapter` | structural adapter-ownership tests; focused adapter/service tests |
| 3. Preserve authority and direct/search/scrape contracts | authority implementation moved with corrected root depth; same-object facades; unchanged entrypoint targets | authority/service/fsearch/fscrape regressions and integration tests |
| 4. Preserve bounded acquisition/failure semantics | bounded adapter implementation moved behind canonical path; shared `provider_preflight` unchanged | issue #216 preflight tests plus acquisition/direct-scrape failure tests |
| 5. Application policy does not select concrete transport | service constructor requires injected `SearchAdapter`; direct service receives adapter factory; composition roots select concrete adapters | issue-specific AST/side-effect tests and builder tests |
| 6. Remove duplicate wrappers only after audit | obsolete concrete implementations removed; active historical module paths retained as facades | semantic caller/reference evidence plus compatibility identity tests |

## Local validation handoff contract

The central implementation must be validated locally at the exact branch HEAD.
The local agent is an execution/evidence layer, not an architecture owner.
Substantive failures return to central ChatGPT for correction; do not redesign
production code or weaken lint/type/test policy locally.

Use the repository validation order from `references/local-agent-validation.md`:

1. Determine changed Python files from the authoritative #262 base and run
   changed-scope `ruff check` plus `ruff format --check --diff`.
2. Run `pyrefly check <changed.py ...>` including changed tests. Pyrefly is
   pinned by `requirements-typecheck.txt`; do not substitute another type
   checker or update `pyrefly-baseline.json`.
3. Run focused deterministic pytest for the acquisition slice.
4. Run full-project `pyrefly check` with no file arguments.
5. Run relevant broader acquisition/PostgreSQL/package gates.
6. Run `git diff --check` and report exact base/head SHAs.

Focused tests should include at minimum:

```text
scripts/test_issue_262_acquisition_slice.py
scripts/test_acquisition_service.py
scripts/test_acquisition_authority.py
scripts/test_issue_216_extraction_preflight.py
scripts/test_direct_scrape_service.py
scripts/test_authoritative_fsearch.py
scripts/test_authoritative_fsearch_review.py
scripts/test_authoritative_fscrape.py
scripts/test_authoritative_fscrape_cli.py
scripts/test_postgres_acquisition_repositories.py
scripts/test_package_boundary.py
```

PostgreSQL-backed tests must use the configured disposable test environment.
Do not invent credentials. Report an unavailable integration environment as a
skip/gap rather than silently substituting a different authority.
