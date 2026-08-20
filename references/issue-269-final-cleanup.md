# Issue #269 final compatibility-cleanup contract

This is the authoritative implementation/handoff ledger for PR #292. Central
owns architecture, mappings, acceptance interpretation, and the deterministic
finalizer. The local agent is limited to executing that finalizer, inspecting
its fail-closed output, formatting, running static/runtime authorities, and
returning evidence. It must not redesign modules, recreate facades, weaken
checks, regenerate the Pyrefly baseline, or change runtime policy.

## Final package rule

Production code is installed exclusively from `src/firecrawl_skill`. `scripts/`
contains executable/operator or fixture support only and is never a setuptools
production-module root.

## Canonical ownership

| Legacy identity | Final owner |
|---|---|
| top-level `budget_policy` | `research_store.budget_policy` |
| top-level `candidate_ranking` | `research_store.acquisition.candidate_ranking` |
| top-level `classifier` | `research_store.acquisition.classifier` |
| top-level `model_gateway` | `firecrawl_skill.model_gateway` |
| generic `research_store.service` | symbol owners in `corpus_service`, `domain`, `assessment.*`, `export_serialization` |
| `container` / `orchestration.composition` | `research_store.composition` |
| flat acquisition facades | `research_store.acquisition.*` and `research_store.composition` |
| flat assessment facades | `research_store.assessment.*` |
| flat reporting facades | `research_store.reporting.*` |
| flat release facades | `research_store.release.*` |
| flat retrieval/projection facades | `research_store.retrieval.*` and `retrieval.projection.*` |
| root `cli.py` facade | `research_store.cli` package |

`research_store.ports` remains because it owns substantial cross-capability UoW
and repository protocols, but its historical `SearchAdapter` re-export is
removed. `research_store.postgres_audit` remains because it is a substantive
connection-bound repository, not a migration facade.

The package-root `FirecrawlSearchAdapter` alias is also removed; callers use
`acquisition.adapters.bounded_firecrawl.BoundedFirecrawlSearchAdapter`.

## Active compatibility behavior retained by justification

Not every occurrence of the word “compatibility” is migration scaffolding. The
`PostgresUnitOfWork.persist_ingest` and issue-#217 ingestion-batch installation
in `research_store.__init__` remain active campaign/runtime contracts with
behavioral tests. They are not alternate package ownership or import facades and
are therefore documented exceptions to module-facade removal. Removing them
would be a separate behavioral API change outside #269.

## Report construction physical move

`report_service.py` moves intact to `reporting/construction.py`. The move changes
only package-relative import resolution and schema-root depth. The finalizer
rewrites imports to explicit canonical owners and resolves schemas from
`Path(__file__).resolve().parents[4] / "schemas" / "research-workflow"`.
`LocalSynthesisService.__module__` must consequently be
`firecrawl_skill.research_store.reporting.construction`.

## Domain dependency correction

`research_domain.assessment.BenchmarkResult.to_dict()` must use
`research_domain.codec.to_dict`; the domain package must not depend upward on a
store-layer evidence facade merely for serialization.

## Pyrefly rule

- Checker version, configuration, project scope and baseline semantics remain
  fixed.
- The finalizer removes baseline entries **only when their recorded path no
  longer exists after physical deletion**.
- It never re-keys a diagnostic to a new path and never regenerates the
  baseline.
- Diagnostics at surviving/new canonical files remain visible. They must be
  fixed or explicitly reviewed against the pre-cleanup normalized diagnostic
  identity; no new debt may be hidden.

## Codex / automated review status

The authoritative PR #292 review surface exposes no Codex-authored review,
inline thread, or conversation comment. The only formal review is Central's
stale `CHANGES_REQUESTED` review on the old head. A historical #267 document had
recorded a Codex thread near `orchestration/composition.py`, but its body was
not captured. Its architectural class is resolved conservatively: application
orchestrator builders use the narrow `production_topology` leaf rather than a
composition compatibility facade. No missing Codex wording is inferred or
fabricated.

## Deterministic local finalization

The Central-owned helper is `.refactor/issue_269_finalize.py`. Before running it:

1. fetch the PR branch;
2. checkout `refactor/compat-cleanup` at the exact SHA supplied by Central;
3. require `git status --porcelain` to be empty;
4. run the helper first without `--apply` to verify its exact-head precondition;
5. run the same helper with `--apply`.

The helper performs only these predetermined operations:

- AST-aware import rewrites from legacy identities to the owners in this file;
- dynamic patch/import-target rewrites for the same identities;
- the report-construction physical move and schema-root correction;
- domain `BenchmarkResult.to_dict()` codec correction;
- removal of root `FirecrawlSearchAdapter` and `ports.SearchAdapter` aliases;
- workflow path-filter migration;
- physical deletion of all #269 migration-only facades and duplicate script
  implementations;
- pruning of Pyrefly baseline entries whose old path was physically deleted;
- final forbidden-import/dynamic-target/path census and `git diff --check`;
- self-deletion only after all finalizer invariants pass.

A nonzero finalizer exit is evidence of an unresolved mapping. The local agent
must stop and return the exact violation to Central; it must not invent another
facade or choose a new owner.

## Required post-finalizer evidence

Local execution is not acceptance. Return all of the following, bound to the
post-finalizer commit SHA:

- raw `git rev-parse HEAD`, base SHA, and complete changed-file list;
- Serena reference census confirming every deleted facade has zero supported
  callers and canonical owners have the expected references;
- `git diff --check`;
- `ruff check` and `ruff format --check --diff` over exact changed Python paths,
  followed by repository Ruff authority;
- Pyrefly on exact changed Python paths and then full-project `pyrefly check`;
- before/after baseline counts plus normalized evidence that no new diagnostic
  identity was hidden or re-keyed;
- final topology, package/wheel, #262 acquisition, #263 retrieval/projection,
  #264 assessment/reporting, #267 composition, checkpoint, reconciliation,
  fsearch/fscrape, CLI, release and audit authorities;
- all PostgreSQL-reset/Qdrant-mutating tests through
  `scripts/disposable-test-services`, including reset and teardown evidence;
- exact-head GitHub CI re-read after the local mechanical commit.

No merge, PR-ready transition, issue closure, or phase-gate closure follows
implicitly from this handoff. Central must re-review the final exact head.
