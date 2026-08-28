# PR #281 orchestration-boundary review remediation

> **HISTORICAL / NON-NORMATIVE.** This review record predates epic #309's deterministic `fresearch` control plane. Statements below about `fsearch_smart` owning orchestration, checkpoint output, or generated recovery commands describe the historical implementation only. Current runtime authority is `SKILL.md` plus `references/authoritative-workflows.md` and `references/workflow-state-schema.md`.

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
`orchestration.ports.ResumeOrchestratorPort` now defines the narrow application
surface consumed by `run_resume`, including its private stage/control hooks and
required composed services. `orchestration.resume` imports only that canonical
port and has no runtime **or type-only** dependency on `smart_orchestrator`.
`smart_orchestrator` retains the application facade needed for resumable
behavior, but not a production composition builder.

Dependency direction remains:

```text
smart application facade -> canonical resume -> resume support / ports
```

with no canonical-resume import back into the facade and no application import
of `research_store.composition`. Type-checking imports are subject to the same
boundary rule as runtime imports; they are not a compatibility escape hatch.

**Regression.** Boundary tests assert that canonical resume does not depend on
`smart_orchestrator` and that resume support contains no raw database access or
facade imports. Full-project Pyrefly additionally validates that the structural
`ResumeOrchestratorPort` matches `ResumableResearchOrchestrator` without
suppressions or baseline expansion.

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

## Temporal coverage gaps and resume (issue-307)

Issue-307 makes temporal evidence insufficiency a typed, recoverable condition
without allowing unrelated evidence failures to enter that recovery path:

- **Interpretation is semantic-primary in autonomous mode.** Before planning,
  `smart-objective-intent-v1` decomposes the exact raw objective into bounded
  research questions, entities, jurisdictions, user constraints, and temporal
  semantics. Deterministic code validates cross-field invariants, assigns IDs,
  performs date arithmetic, and materializes the authoritative `ResearchSpec`.
  Query planning receives that materialized semantic scope rather than a fresh
  generic one-question brief.
- **Autonomous semantic failure fails closed.** Normal `autonomous_local`
  execution never falls through to regex parsing after model/schema/semantic
  failure. The deterministic grammar in `fallback_temporal_spec` is available
  only in explicit `deterministic_debug`/degraded operation and remains narrow.
- **Evidence and discovery time remain independent.** A freshness objective may
  materialize `max_age_days` without a publication window while retaining a
  rolling discovery interval. Provider recency is only a downstream
  non-narrowing discovery projection.
- **The canonical evidence boundary owns the gap type.**
  `EvidencePreparationService` raises `TemporalCoverageUnsatisfied` with
  `TemporalCoverageDiagnostics` when temporal obligations exist and the bounded
  authoritative passage set has zero qualifying passages. Qualification and
  diagnostics share one explicit evaluation clock. Multiple freshness
  obligations are conjunctive, so stale diagnostics use their strictest age.
- **Generic evidence errors cannot be reclassified.** The typed temporal class
  deliberately does not inherit `EvidencePreparationError`. Smart resume catches
  only `TemporalCoverageUnsatisfied`; semantic claim-extraction failures, packet
  validation failures, malformed coverage state, and other ordinary evidence
  errors retain their normal failure semantics even if the current corpus would
  independently fail a temporal predicate. `_temporal_gap_from_authority` remains
  diagnostic-only and is not a production error classifier.
- **A gap is persisted, never auto-relaxed.** The bounded diagnostic payload is
  recorded as `evidence.temporal_coverage_gap`. While adaptive budget remains,
  normal coverage strategy can reacquire. At exhaustion the run returns
  `operator_action_required` without terminalizing or waiving temporal evidence.
  Scope changes require an explicit persisted `ResearchSpec` revision.
- **Resolution is explicit.** A temporal operator disposition exits 75 with
  `resolve_temporal_coverage_gap_then_resume_same_run`. Its bounded summary
  includes basis, qualifying/examined census, non-zero reason classes,
  `automatic_scope_relaxation=false`, and the required resolution. Candidate
  budget action uses `resolve_candidate_budget_override_then_resume_same_run`.
  An unrecognized future operator-action kind fails safely to
  `inspect_operator_action_then_resume_same_run` rather than guessing a repair.
- **Successful reacquisition closes the durable gap.** Once evidence preparation
  succeeds after a prior gap, resume persists
  `evidence.temporal_coverage_resolved` and continues through the same legal run
  lifecycle.

The exact issue-307 acceptance mapping and validation targets are recorded in
`references/audit-remediation-307.md`.

## Acceptance conditions

The current orchestration/composition boundary is considered validated only when
an exact candidate head satisfies:

1. changed-scope Ruff and formatting;
2. changed-scope and full-project repo-pinned Pyrefly;
3. focused orchestration and #267/#307 composition/recovery tests;
4. applicable PostgreSQL/service-backed tests using repository-sanctioned
   disposable services;
5. existing PR CI/check authorities;
6. exact-head readback showing the PR still points at the tested commit.

No merge is part of this remediation record.
