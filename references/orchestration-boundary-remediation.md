# PR #281 orchestration-boundary review remediation

This note records the remediation applied to PR #281 for issue #261 after an
independent exact-head review of `fbc2dfb2c57bc621fd2f655cec12db31b1e380fc`.

The remediation is deliberately narrow: it does not restore the former
`research_store.__init__` import-time orchestrator monkeypatches and does not
consolidate acquisition policy beyond issue #261. Production topology remains
explicit.

## Review finding → implementation → regression

### 1. Smart production construction bypassed bounded/checkpoint composition

**Defect.** `fsearch_smart` constructed
`ProvenanceResumableResearchOrchestrator.build(...)` directly after the old
module-level rebinding was removed. The inherited builder therefore resolved
the base acquisition/extraction stages, and smart resume no longer inherited
checkpoint execution/failure behavior.

**Implementation.**

- `CheckpointResearchOrchestrator.build()` now defaults its public/historical
  construction surface to `BoundedAcquisitionStage` and
  `BoundedExtractionStage`, while honoring explicit stage overrides. This
  preserves the pre-refactor production default without mutating module state.
- `ResumableResearchOrchestrator` now explicitly subclasses
  `CheckpointResearchOrchestrator`. Checkpoint semantics are therefore visible
  in the class hierarchy rather than supplied by package-import mutation.
- `ProvenanceResumableResearchOrchestrator.build()` also defaults direct
  historical smart callers to the bounded stage pair.
- `orchestration.composition.build_production_resumable_orchestrator()` is the
  explicit smart production composition root.
- `fsearch_smart.execute()` uses that composition root.

**Regression.** `test_issue_261_review_remediation.py` constructs the actual
provenance builder with checkpoint support enabled and asserts runtime
instances of the bounded acquisition/extraction stages and
`CheckpointIndexingStage`, plus checkpoint method dispatch. A separate caller
test verifies `fsearch_smart` routes through the resumable composition root.

This also addresses the Codex automated review thread attached to the
orchestrator builder/caller path.

### 2. Accepted adaptive strategy order changed on resume

**Defect.** The historical resume SQL returned accepted search proposals in
ascending `revision_order`. The first repository-based implementation called
the generic `list_proposals()`, which is intentionally descending, and then
performed decision lookups per proposal. Under `max_search_branches`, that
could execute a different authorized query subset after restart.

**Implementation.**

`PostgresStrategyRevisionRepository.list_accepted_search_proposals()` now owns
one canonical SQL projection that:

- filters to `row_type='proposal'` and `decision_type='search'`;
- requires an accepted decision with `EXISTS`;
- returns only proposals with queries;
- orders by ascending `p.revision_order`;
- executes as one PostgreSQL statement.

`PostgresResumeStateReader.authorized_queries()` delegates directly to this
projection. The adapter owns neither SQL nor strategy policy, and the prior
single-statement snapshot semantics are preserved.

**Regression.** The PostgreSQL test seeds an older accepted proposal, a rejected
proposal, and a newer accepted proposal. It proves exact accepted-only
ascending reconstruction, then deliberately binds the search-branch cap and
proves the older authorized query is the one that executes. A mock-level test
also prevents reintroduction of the prior N+1 adapter reconstruction.

This also addresses the Codex automated review thread attached to
`PostgresResumeStateReader.authorized_queries()`.

### 3. Canonical resume use case depended back on the compatibility facade

**Defect.** `orchestration.resume` imported private reconstruction helpers and
resume contracts from `smart_orchestrator`, while `smart_orchestrator.run()`
delegated back into `orchestration.resume`.

**Implementation.**

`orchestration.resume_support` now owns:

- resume state constants;
- `SmartResumeError`;
- deterministic coverage-context reconstruction;
- deterministic extraction-input replay.

The replay helper receives the completed-candidate set from `ResumeStatePort`;
it performs no infrastructure read itself. `smart_orchestrator` retains only
thin compatibility aliases/wrappers for historical imports. Dependency
direction is now:

`smart facade -> canonical resume -> resume support / ports`

with no canonical-resume import back into the facade.

**Regression.** Boundary tests assert that `orchestration.resume` does not
depend on `smart_orchestrator` and that `resume_support` contains neither raw
database access nor facade imports.

## Test/documentation gaps closed

A dedicated `Orchestration boundary remediation` GitHub Actions workflow now
runs on both Python 3.11 and 3.12 with disposable PostgreSQL. It executes:

- `scripts/test_issue_261_review_remediation.py`;
- `scripts/test_orchestration_package.py`.

The focused #261 suite is therefore exact-head CI evidence rather than
local-only evidence.

The new PostgreSQL regression adds the two previously missing resume-port
projections:

- accepted adaptive query reconstruction/order;
- latest evidence-packet revision.

The production topology regression is behavioral. It does not rely on merely
searching the composition-root source for bounded class names.

## Authority and scope preservation

The remediation does not change:

- `ExecutionModePolicy`;
- `CoverageService` policy;
- `StrategyRevisionService` authorization policy;
- terminal-decision policy or atomic terminal transition ownership;
- PostgreSQL unit-of-work commit/rollback/savepoint ownership.

The new strategy projection is connection-bound to the existing Phase-3 UoW.
It performs no commit, rollback, savepoint, or independent connection
acquisition.

## Acceptance conditions

The remediation is complete only when the exact remediation head satisfies:

1. focused orchestration-boundary CI on Python 3.11 and 3.12;
2. existing PR CI/checks;
3. Ruff and formatting gates;
4. exact-head readback showing the PR still points at the tested commit.

No merge is part of this remediation.
