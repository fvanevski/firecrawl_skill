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

The Central-prepared tree contains a P5-era `reporting/construction.py` re-export
stub. That stub predates the final #269 ownership decision and is not a second
implementation. Revision-2 finalization accepts it only when its complete UTF-8
contents exactly match the reviewed 383-byte stub, removes it before the import
rewrite census, and then performs the prescribed physical move. Any missing,
modified, or otherwise different target still fails closed; the local agent has
no authority to overwrite an unknown target.

## Deterministic fixture disposition

`scripts/fixtures/` remains test/support space and is intentionally excluded from
production ownership. Two tracked fixture symlinks historically pointed to the
top-level `scripts/` implementations removed by #269:

- `scripts/fixtures/classifier.py -> ../classifier.py`
- `scripts/fixtures/model_gateway.py -> ../model_gateway.py`

Those links are not deleted because current deterministic tests still consume
them. `scripts/fixtures/workflow_test_cases.py` loads both fixtures as isolated
modules, and CI explicitly type-checks the model-gateway fixture.

Revision-2 finalization verifies both original links and targets exactly before
mutation, then migrates them **before** the core rewrite/deletion pass:

- `classifier.py` becomes a small regular fixture module that imports the public
  classifier surface from
  `firecrawl_skill.research_store.acquisition.classifier`. A direct symlink is
  not valid here because the canonical classifier uses a package-relative
  `candidate_ranking` import and the fixture is loaded under an isolated module
  name.
- `model_gateway.py` remains a symlink fixture but is repointed to
  `../../src/firecrawl_skill/model_gateway.py`. The canonical gateway uses
  package-absolute dependencies, so isolated `SourceFileLoader` execution
  remains valid and fixture-local monkeypatching of transport globals keeps the
  existing test semantics.

The final topology contract requires the classifier fixture to be a non-symlink
canonical wrapper and the model-gateway fixture symlink to resolve to the
canonical source. Neither fixture may remain dangling or target a deleted
production duplicate.

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
- The two existing records keyed to
  `src/firecrawl_skill/research_store/report_service.py` are therefore removed
  only because that path disappears. They are not copied to
  `reporting/construction.py`.
- Diagnostics at the moved `reporting/construction.py` or any other surviving
  canonical file remain visible as non-baselined diagnostics and must be fixed
  or explicitly returned to Central for review. No new debt may be hidden.

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

The original Central mappings and mutation functions remain in
`.refactor/issue_269_finalize.py`. Because that helper's initial AST pass would
rewrite the known P5 report-construction stub before reaching its move guard,
the executable handoff entry point is now
`.refactor/issue_269_finalize_v2.py`.

The v2 driver is intentionally thin. It imports the original helper as its
Central-owned core, verifies exact source identity and a clean worktree, verifies
the known report-construction stub byte-for-byte, verifies the two known fixture
symlinks and their live legacy targets, removes only the reviewed report stub,
migrates the fixture shims to their canonical support forms, and then invokes
the original predetermined mutation functions in their intended order. On
successful final-state verification it deletes both temporary helper files so
the local mechanical commit contains no migration machinery.

Before running it:

1. fetch the PR branch;
2. checkout `refactor/compat-cleanup` at the exact SHA supplied by Central;
3. require `git status --porcelain` to be empty;
4. run `issue_269_finalize_v2.py` first without `--apply`;
5. require `known_reporting_stub_verified: true`;
6. require both fixture paths under `known_fixture_symlinks_verified`;
7. run the same v2 helper with `--apply`.

The v2 driver and core together perform only these predetermined operations:

- exact verification and retirement of the reviewed P5 report-construction stub;
- exact migration of the two legacy-target fixture symlinks described above;
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
- deletion of both finalizer helpers only after all finalizer invariants pass.

Do not run `.refactor/issue_269_finalize.py` directly on the revised Central
head. It is the implementation core used by the v2 driver.

A nonzero v2 finalizer exit is evidence of an unresolved mapping or changed
precondition. The local agent must stop and return the exact violation to
Central; it must not invent another facade, alter the target guard, repair a
fixture outside the encoded migration, or choose a new owner.

## Required post-finalizer evidence

Local execution is not acceptance. Return all of the following, bound to the
post-finalizer commit SHA:

- raw `git rev-parse HEAD`, base SHA, and complete changed-file list;
- Serena reference census confirming every deleted facade has zero supported
  callers and canonical owners have the expected references;
- explicit confirmation that both retained fixture paths resolve exactly as the
  final topology contract requires;
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
