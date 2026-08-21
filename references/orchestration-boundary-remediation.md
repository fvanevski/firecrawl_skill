# PR #281 orchestration-boundary review remediation

This note records the remediation originally applied to PR #281 for issue #261
after independent exact-head review. Phase-5 issues #267 and #269 later
centralized production construction and removed migration composition surfaces.
The historical discussion below is retained only where it explains why the
current architecture exists; current ownership is stated explicitly.

The remediation remains deliberately narrow: it does not restore the former
`research_store.__init__` import-time orchestrator monkeypatches and does not
move workflow policy into composition. Production topology remains explicit.

## Review finding -> implementation -> regression

### 1. Smart production construction bypassed bounded/checkpoint composition

**Original defect.** `fsearch_smart` constructed
`ProvenanceResumableResearchOrchestrator.build(...)` directly after the old
module-level rebinding was removed. The inherited builder could resolve the base
acquisition/extraction stages, and smart resume could lose checkpoint/bounded
production behavior.

**Historical #261 remediation.** Subclass `build()` methods temporarily
preserved bounded/checkpoint defaults while composition was still being
normalized.

**Current Phase-5 implementation.** Those subclass builders are no longer
supported production surfaces:

- `ResearchOrchestrator`, `CheckpointResearchOrchestrator`, and
  `ProvenanceResumableResearchOrchestrator` accept already-composed
  collaborators and expose no config-driven `build()` method;
- `research_store.composition.build_production_orchestrator()` and
  `build_production_resumable_orchestrator()` are the canonical fresh/smart
  `StoreConfig`-driven production builders;
- the former `research_store.orchestration.composition` migration facade is
  absent after #269 rather than import-compatible;
- `research_store.production_topology.ProductionBoundedExtractionStage` is a
  narrow leaf used by the canonical root. It injects the bounded Firecrawl
  adapter but owns no config/service/UoW resolution or workflow policy;
- `fsearch_smart` calls the canonical resumable production builder.

The current dependency direction is therefore construction-root outward only;
application/orchestrator modules do not call back into the root.

**Regression.** The orchestration and #267 boundary tests require runtime
bounded-stage selection through the canonical root, absence of the old class
builders, and absence of application-to-composition back-edges.

### 2. Accepted adaptive strategy order changed on resume

**Original defect.** The historical resume SQL returned accepted search
proposals in ascending `revision_order`. The first repository-based
implementation called the generic descending proposal listing and then performed
decision lookups per proposal. Under `max_search_branches`, that could execute a
different authorized query subset after restart.

**Implementation.**

`PostgresStrategyRevisionRepository.list_accepted_search_proposals()` owns the
canonical SQL projection that:

- filters to proposal/search rows;
- requires an accepted decision;
- returns only proposals with queries;
- orders by ascending proposal revision;
- executes as one PostgreSQL statement.

`PostgresResumeStateReader.authorized_queries()` delegates to this projection.
The adapter owns neither SQL nor strategy policy and the single-statement
snapshot semantics are preserved.

**Regression.** PostgreSQL tests seed accepted/rejected proposals and prove
accepted-only ascending reconstruction, including behavior under the search
branch cap. Mock-level coverage prevents reintroduction of the prior N+1 reader
reconstruction.

### 3. Canonical resume use case depended back on the compatibility facade

**Original defect.** `orchestration.resume` imported private reconstruction
helpers and resume contracts from `smart_orchestrator`, while
`smart_orchestrator.run()` delegated back into `orchestration.resume`.

**Implementation.** `orchestration.resume_support` owns resume state constants,
`SmartResumeError`, deterministic coverage-context reconstruction, and
deterministic extraction-input replay. The replay helper receives completed
candidate state through `ResumeStatePort`; it performs no infrastructure read.
`smart_orchestrator` retains the application facade needed for resumable
behavior, but not a production composition builder.

Dependency direction remains:

```text
smart application facade -> canonical resume -> resume support / ports
```

with no canonical-resume import back into the facade and no application import
of `research_store.composition`.

**Regression.** Boundary tests assert that canonical resume does not depend on
`smart_orchestrator` and that resume support contains no raw database access or
facade imports.

## Phase-5 composition interaction

Issue #267 does not change orchestration policy, lifecycle, checkpoint or resume
semantics. It changes where construction dependencies live:

```text
operator / entrypoint
  -> research_store.composition
       -> CheckpointResearchOrchestrator / ProvenanceResumableResearchOrchestrator
       -> production_topology.ProductionBoundedExtractionStage
```

The following back-edges are forbidden and covered by Phase-5 regressions:

```text
checkpoint/search-provenance implementation -> research_store.composition
orchestrator/application service -> research_store.composition
```

The old `research_store.orchestration.composition` path is removed; it is not a
current compatibility or architectural surface.

## Test/documentation gaps closed

The dedicated orchestration-boundary workflow continues to exercise the #261
behavioral regressions on supported Python versions. The Phase-5 composition
regression adds import-direction and construction-ownership checks without
weakening the existing behavioral tests.

The PostgreSQL regressions continue to cover the previously missing resume-port
projections:

- accepted adaptive query reconstruction/order;
- latest evidence-packet revision.

Production topology validation is not limited to source-name searching: tests
exercise the canonical fresh/resumable composition builders and verify the
bounded/checkpoint stage classes actually installed, while #267 separately
constrains the leaf topology module itself.

## Authority and scope preservation

Neither #261 nor the Phase-5 composition cleanup changes:

- `ExecutionModePolicy`;
- coverage or strategy authorization policy;
- terminal-decision policy or atomic terminal transition ownership;
- PostgreSQL unit-of-work commit/rollback/savepoint ownership;
- persisted schema or CLI semantics.

The strategy projection remains connection-bound to the existing Phase-3 UoW.
The Phase-5 `production_topology` leaf performs no database work.

## Acceptance conditions

The current orchestration/composition boundary is considered validated only when
an exact candidate head satisfies:

1. changed-scope Ruff and formatting;
2. changed-scope and full-project repo-pinned Pyrefly;
3. focused orchestration and #267 composition tests;
4. applicable PostgreSQL/service-backed tests using repository-sanctioned
   disposable services;
5. existing PR CI/check authorities;
6. exact-head readback showing the PR still points at the tested commit.

No merge is part of this remediation record.
