# Phase 5 exact-main remediation after `b6700a06...`

## Authority and status

Issue #266 was freshly re-evaluated locally with Codex CLI / `gpt-5.6-sol` high
against exact authoritative main:

`b6700a06e897b6651f737de4994b1acae8d4d2ca`

The local result was `LOCAL_GATE_EVIDENCE=FAIL`. This document records the
substantive Central remediation that must be complete **before** the next local
agent validation. Evidence collected on `b670...` is defect-discovery evidence,
not acceptance evidence for any later PR head.

## Blocking findings and Central resolutions

### 1. Application -> composition dependency direction

Fresh Serena/current-source inspection found root back-edges in
`ResearchOrchestrator.build`, `fscrape_service`, `fsearch_policy_service`,
`inspection_service`, and `ResearchRunService.trigger_audit`. This directly
violated `references/composition-root.md`; function-local imports were explicitly
forbidden.

Central resolution:

- production service builders are root-owned in `research_store.composition`;
- `fscrape_service`, `fsearch_service`, `fsearch_policy_service`, and
  `inspection_service` no longer construct through or import the root;
- CLI/operator boundaries call root-owned builders;
- `ResearchRunService` receives an audit-service factory from composition;
- config-driven `ResearchOrchestrator.build` and subclass `build` facades are
  removed;
- `composition.build_orchestrator_instance` assembles injected collaborators and
  production stage classes;
- `tests/unit/test_issue_267_composition_root.py` mechanically rejects recurrence
  across the affected application modules, requires root-owned builder names to
  have one owner, and audits executable callers for deleted class-level builders.

### 2. `scripts/fsearch_smart` ordinary production implementation

The local audit found budget evaluation, canonical plan construction, planning-
bundle initialization, and direct planner-provenance UoW persistence in the
operator executable.

Central resolution:

- reusable behavior moved to `research_store.smart_search_application`;
- the script retains only operator concerns and a planner adapter over
  `scripts/research_workflow.py`;
- planner provenance append/commit is application-owned;
- the Phase-5 remediation contract asserts that the removed application functions
  do not reappear in the script and that it performs no direct `append_event` or
  `commit` calls;
- smart-search integration tests import reusable plan construction from the
  installed application owner while retaining script-level tests for actual
  operator behavior.

### 3. Exact topology census `137 != 136`

The extra active path came from the Phase-5 remediation contract while
`tests/contract/test_release_invariants.py` remained collected despite declaring
itself globally skipped and superseded.

Central resolution:

- the superseded release-invariant module is a test-free tombstone instead of a
  module-level skip surface;
- its canonical replacement remains
  `tests/contract/test_release_invariant_contracts.py` plus PostgreSQL integration
  authorities;
- the exact behavior/boundary census remains **136**; it is not loosened or
  mechanically inflated;
- `test_test_topology.py` verifies the tombstone has no test functions and no
  `pytest.mark.skip`, and verifies the canonical owner exists.

### 4. Eighteen unknown full-suite skips

All 18 unknown skips originated from the same superseded release-invariant module.
The resolution above removes those duplicate collected tests rather than adding
18 allowlist exceptions. The next local full-suite run must still prove zero
unknown/stale/reason-mismatched skips.

### 5. Stale test-authority consumers exposed by PR-head CI

Exact PR head `a3e9182e7b3be8d1202fb4e9e84a4aa7f8e6a155` had clean Ruff,
pinned Pyrefly, release invariants, orchestration, fscrape, acquisition, retrieval,
checkpoint, reconciliation, RC-10, and related focused authorities. Its remaining
fsearch/storage/broad-suite failures were stale tests rather than production
compatibility defects:

- `tests/integration/test_authoritative_smart_validation.py` still reached
  `canonical_plan` and planning-bundle initialization through extensionless
  `scripts/fsearch_smart` after those reusable responsibilities moved to
  `smart_search_application`;
- `tests/integration/test_research_store_integration.py` still invoked removed
  `ResearchOrchestrator.build` instead of asking the canonical composition root
  to assemble the application object.

Central resolution preserves the assertions while correcting ownership:

- canonical-plan validation imports `smart_search_application.canonical_plan`;
- script preflight/resume tests patch the current
  `initialize_planning_bundle` operator seam rather than deleted helper names;
- the base-orchestrator PostgreSQL integration is constructed through
  `composition.build_orchestrator_instance(ResearchOrchestrator, ...)`;
- the #267 composition regression scans executable source/test callers and fails
  if a deleted orchestrator class-level `build` facade is called again.

The `a3e...` CI result is defect-discovery evidence only. Any commit implementing
these corrections invalidates that CI and requires fresh exact-head evidence.

## Important but non-blocking findings

### Structural metrics

Phase-1 -> Phase-5 metrics remain review evidence only. The `b670...` values
(modules 195 -> 269, physical LOC 93,441 -> 90,399, top-level symbols 1,222 ->
1,331) are stale after this remediation because application ownership moved and
new package/CLI modules were added. Do not compare a later PR result by copying
those current values; regenerate the artifact with the exact handoff SHA.

### Validation identity

Every Central commit advances the PR head and invalidates earlier PR-head CI,
review, structural metrics, and local evidence. A local handoff is authorized
only after Central has completed source/test/document changes and the exact PR
head is stable enough for validation.

### Deferred-construction observability

The local `3ed7ae2c...` audit correctly observed that moving construction into the
canonical root had silently lost debug-level diagnostics for optional corpus and
extraction auto-build failures. The root now logs those deferred construction
exceptions before preserving the historical fail-soft `None` behavior. This is
observability parity, not a second composition path.

## Automated Codex review dispositions

### PR #294 current-head review authority

At exact head `a3e9182e7b3be8d1202fb4e9e84a4aa7f8e6a155`, GitHub's authoritative
review surface reported zero formal reviews, zero review threads, zero requested
reviewers, and no current-head comments/change requests/approvals. Therefore
there is no PR-#294-specific Codex automated suggestion to disposition at that
revision. A later automated review is new evidence and must be read and resolved
against the exact head on which it is submitted; this record must not be used to
pre-dismiss future review findings.

### Inherited PR #292 extensionless `fsearch_smart` suggestion

The late Codex review correctly identified that extensionless Python had escaped
the earlier `.py`-only final-owner audit. PR #293 fixed the removed imports and
made direct Ruff/Pyrefly checks CI-owned. The `b670...` gate pass then exposed the
broader ownership problem. This remediation completes that finding in substance
by extracting reusable planning/persistence behavior while retaining direct
extensionless static authority.

### Inherited PR #293 duplicate retired-module inventory suggestion

The stale automated review objected to duplicating removed-module names in the
new remediation contract. Current code remains on the corrected design: the test
reads `FORBIDDEN_MODULES` from
`tests/contract/test_issue_269_final_topology.py` by AST and applies exact module
or descendant matching. No duplicate literal inventory is reintroduced.

### Inherited acquisition bounded-stage suggestion

The historical acquisition review identified a path by which subclass-owned
construction could select bounded extraction without the required candidate
adapter. The final Phase-5 model supersedes the subclass builder entirely:
`research_store.composition` selects `ProductionBoundedExtractionStage`, and the
narrow `production_topology` leaf injects `BoundedFirecrawlSearchAdapter`.
Behavioral acquisition/orchestration authorities retain coverage of that path.

## Test/documentary gaps closed

The remediation adds or strengthens contracts for:

- package-level application -> composition dependency direction;
- single ownership of final production builder names;
- absence of config-driven orchestrator builders in both definitions and
  executable callers;
- smart-search operator/application responsibility separation;
- canonical-owner smart-search tests after reusable behavior left the operator
  executable;
- retirement of superseded skipped test authority without weakening the exact
  test census;
- preservation of extensionless Ruff/Pyrefly ownership;
- preservation of AST-derived #269 retired-module authority;
- stale historical documents that previously described removed subclass builders,
  compatibility facades, or acquisition hooks as current surfaces;
- deferred-construction debug observability preserved through root centralization.

`references/composition-root.md`, `references/orchestration-boundary-remediation.md`,
and `references/acquisition-slice-review-remediation.md` describe the final
injected-collaborator model rather than the superseded compatibility state.

## Required local handoff validation

The next local Codex CLI / `gpt-5.6-sol` high pass must be bound to the exact PR
head supplied by Central. It must not modify substantive source/tests/docs.

At minimum return:

1. raw native Git `HEAD`, `origin/<branch>`, clean-tree, and merge-base evidence;
2. Serena `no-memories` census proving no application/service back-edge to
   `research_store.composition`, with explicit inspection of orchestrator,
   checkpoint/search-provenance subclasses, run service, fsearch/fscrape/
   inspection, `smart_search_application`, and `scripts/fsearch_smart`;
3. repository Ruff/format plus direct extensionless lint/import-order/format;
4. repository-pinned Pyrefly `1.1.1` on changed scope, full project, and direct
   extensionless executable, with baseline/config unchanged;
5. focused tests including:

   ```text
   tests/unit/test_issue_267_composition_root.py
   tests/contract/test_test_topology.py
   tests/contract/test_phase5_gate_remediation.py
   tests/contract/test_issue_269_final_topology.py
   tests/contract/test_pyrefly_gate.py
   tests/contract/test_exact_head_ci_evidence.py
   tests/integration/test_authoritative_smart_validation.py
   ```

6. the canonical-root `TestResearchOrchestratorIntegration` PostgreSQL path from
   `tests/integration/test_research_store_integration.py`;
7. fresh `tools/phase5_architecture_metrics.py --source-sha <EXACT_PR_HEAD>` output;
8. disposable PostgreSQL/Qdrant service contract, including `reset-qdrant` where
   projection state matters and final zero-owned-container cleanup;
9. broad suite plus credential-free skip classification proving no unknown,
   stale, or reason-mismatched skips;
10. final immutable SHA/clean-tree recheck.

Any failure returns to Central. Do not weaken tests, Pyrefly/Ruff scope,
baselines, suppressions, skip policy, package boundaries, persistence authority,
or release evidence to obtain green.
