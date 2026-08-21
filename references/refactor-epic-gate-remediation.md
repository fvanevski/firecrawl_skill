# Refactor EPIC gate remediation and local-agent handoff contract

This ledger records the post-merge remediation required after the Refactor EPIC
gate was evaluated against authoritative `main` at:

```text
EPIC_BASE_MAIN_SHA=407c37dd08093ebe26103b1477df05bb100db073
REMEDIATION_BRANCH=gate/refactor-epic-compat-closure
CENTRAL_IMPLEMENTATION_CHECKPOINT=63acb85a379ddfa5b50ed368039f1f7a8a779cde
```

The final handoff SHA is intentionally not hard-coded here because this document
is itself part of the remediation commit history. Central must supply and
re-read the immutable PR head immediately before local execution. Any later
head movement invalidates previous CI, review, and local evidence.

## Blocking findings and disposition

### B1. Migration-era generic UoW persistence routing remained in the final tree

`postgres_uow_core.py` still installed broad direct `uow.<domain operation>`
aliases and multiplexed acquisition, candidate, extraction, and semantic
operations through `uow.runs`. This contradicted the final Phase-5/issue-#269
architecture: the UoW owns transaction lifecycle and exposes explicit
connection-bound repository roles; it is not a generic domain repository.

**Resolved centrally.** The generic compatibility-operation tables,
`_RunsRepository`, `_make_uow_compatibility_delegate`, and
`_bind_uow_compatibility_delegate` were removed. `uow.runs` now binds directly
to `PostgresResearchRepository`; search acquisition, candidates, extraction,
semantic calls, assessment/reporting, retrieval, terminal decisions, corpus,
and other durable state use their explicit repository roles.

The corresponding typed contract was corrected in `ports.py`:
`ResearchRunRepository` no longer inherits search-response, candidate, or
semantic-call protocols, while `UnitOfWork` exposes those capabilities through
separate named roles.

### B2. Production callers still depended on the removed migration router

The initial caller census found cross-domain operations in run/search planning,
acquisition, semantic persistence, assessment/reporting, retrieval, lifecycle,
corpus linkage, and administrative paths. Removing the router alone would have
converted those into runtime `AttributeError` failures.

**Resolved centrally.** Callers were migrated to named repository roles while
preserving transaction scope and behavior. Representative ownership is now:

| Capability | Canonical UoW role |
|---|---|
| lifecycle, invocation, events, ResearchSpec/budget | `uow.runs` |
| search plans/responses | `uow.search_responses` |
| candidates | `uow.candidates` |
| extraction attempts | `uow.extraction_attempts` |
| semantic calls/artifacts | `uow.semantic_calls` |
| claims | `uow.claims` |
| EvidencePackets | `uow.evidence_packets` |
| audit assessments | `uow.audits` |
| synthesis stages | `uow.synthesis_stages` |
| retrieval provenance | `uow.retrieval_events` |
| terminal decisions | `uow.terminal_decisions` |
| run-asset/corpus persistence | `uow.snapshots` |

### B3. Issue #217 retained one hidden dependency on `uow.link_run_asset`

The issue-#217 batch implementation legitimately retains its six published
class-level UoW methods, but its internal corpus path still called the removed
migration-era `uow.link_run_asset` alias. The path would fail whenever a batch
linked an asset to a research run.

**Resolved centrally.** The internal linkage now calls
`uow.snapshots.link_run_asset`. No timing, sealing, membership, rollback,
outcome-summary, or pre-v43 behavior was changed.

### B4. Existing tests positively required obsolete aliases

`tests/integration/test_postgres_final_repository_extraction.py` still asserted
that direct UoW aliases such as `uow.record_semantic_call` existed and that
semantic persistence was reachable through `uow.runs`. Those assertions encoded
the migration state rather than the final architecture.

**Resolved centrally.** The integration test now verifies the inverse invariant:
named repositories own the operations, generic direct aliases are absent, and
`uow.runs` does not expose cross-domain operations. The documented
`persist_ingest` and issue-#217 class APIs remain explicitly tested exceptions.

## Important non-blocking findings and disposition

The following earlier review findings remain architecture constraints but did
not justify further production redesign in this remediation because current
`main` already contains their accepted resolutions:

- Metrics are evidence/observability outputs, not an authority source for
  lifecycle or persistence decisions.
- `production_topology` remains a narrow production leaf rather than a new
  compatibility composition facade.
- Smart-search persistence documentation must reflect its actual separate-UoW
  commits; documentation must not claim a single atomic transaction where none
  exists.
- Any pre-remediation test or CI result is stale after a head change and cannot
  be promoted to exact-head evidence.
- Disposable-service isolation is an evidence requirement rather than a reason
  to change production behavior. The final local report must name the literal
  unique namespace plus selected PostgreSQL and Qdrant endpoints, record any
  `reset-qdrant` action, run `down`, and demonstrate zero owned containers after
  teardown.

No evidence-only metric, local-service convenience, or historical compatibility
expectation may be promoted into runtime authority to make validation green.

## Codex Review items

### Inherited automated-review concerns

Three automated-review classes from the preceding Phase-5/finalization work are
retained as regression requirements:

1. **Extensionless `scripts/fsearch_smart` ownership gap.** Resolved before this
   remediation by moving reusable planning/persistence behavior into installed
   application modules while keeping the extensionless script an operator
   boundary. This remediation must not move implementation ownership back into
   `scripts/`.
2. **Duplicated retired-module inventory.** Resolved by keeping final-topology
   assertion data authoritative rather than creating another mutable runtime
   inventory. `FORBIDDEN_MODULES` in the #269 topology contract is assertion
   data and must not be rewritten as if it were a dynamic import target.
3. **Acquisition bounded-stage/composition concern.** Resolved through the
   canonical composition root and narrow `production_topology` leaf. This
   remediation does not reintroduce an application-to-composition back-edge.

### Current remediation PR

A new remediation PR must be opened only after the central source/test/document
changes are committed. Central must then inspect **all** exact-current-head
reviews, inline review comments/threads, and conversation comments. For each
Codex-authored suggestion, record one of:

- implemented, with exact file/symbol and regression evidence; or
- rejected, with a specific architectural/evidentiary reason.

Zero current-head Codex suggestions is a valid result only when the complete
review surface was actually inspected. Historical text must not be invented to
fill an absent review body.

## Test and documentary gaps closed centrally

`tests/contract/test_issue_269_uow_repository_boundary.py` now provides a
mechanical architecture guard. It:

- scans production ASTs for direct `uow.<domain operation>()` calls and permits
  only transaction infrastructure plus the documented `persist_ingest` and
  issue-#217 class APIs;
- rejects cross-domain acquisition/candidate/extraction/semantic calls through
  `uow.runs`;
- rejects resurrection of the generic compatibility-router implementation;
- verifies the corrected repository protocol inheritance and UnitOfWork role
  annotations; and
- verifies issue-#217 run-asset linkage uses `uow.snapshots`.

The older #259 integration test was migrated to the same final-state invariant.
Package documentation in `research_store.__init__` now describes the retained
class APIs accurately: they remain directly callable behavioral contracts and
are not entered-UoW instance delegates.

This ledger supplements, and does not weaken,
`references/issue-269-final-cleanup.md` and
`references/local-agent-validation.md`.

## Local-agent handoff: execute only after Central says the PR head is frozen

The local agent is an execution/diagnostic authority for host-dependent checks,
not the architectural decision-maker. It must not redesign production behavior
in response to a failing test.

### Tool contract

- **Serena:** first-line semantic navigation, symbol/reference census,
  dependency analysis, diagnostics, and structural inspection. Use no-memories;
  inspect before any mechanical edit and audit after it.
- **RTK:** may compress routine successful Git/Ruff/Pyrefly/pytest output only
  when filtering preserves decisive evidence. Do not use filtered output for
  exact SHAs, complete diffs, failures, migrations, DB/runtime, security, or
  release evidence.
- **OpenViking:** bounded historical rationale only. It is never authority for
  mutable source, GitHub, CI, PostgreSQL, Qdrant, runtime, or release state and
  must not store secrets/raw logs/full diffs.
- **Native tools:** use authenticated native `git`, repository-native commands,
  Docker/system tools, and direct DB/service clients for authoritative local
  state. Do not route local Git/GitHub operations through Central connectors.

### Exact-head precondition

Central supplies:

```text
BASE_SHA=407c37dd08093ebe26103b1477df05bb100db073
REVIEW_HEAD_SHA=<immutable current remediation PR head>
```

The local agent must fetch and detach exactly at `REVIEW_HEAD_SHA`, assert raw
`git rev-parse HEAD` equality, and report the complete
`BASE_SHA...REVIEW_HEAD_SHA` changed-file list before validation.

### Focused structural/static set

At minimum run:

```text
tests/contract/test_issue_269_uow_repository_boundary.py
tests/contract/test_issue_269_final_topology.py
tests/integration/test_postgres_final_repository_extraction.py
tests/contract/test_package_boundary.py
tests/contract/test_pyrefly_gate.py
tests/contract/test_issue_262_acquisition_slice.py
tests/unit/test_issue_263_retrieval_projection_slice.py
tests/contract/test_issue_264_assessment_reporting_slice.py
tests/unit/test_issue_267_composition_root.py
tests/contract/test_index_checkpoint_contract.py
tests/contract/test_asset_promotion_contract.py
```

Then perform changed-scope Ruff and Ruff format checks, changed-scope Pyrefly,
focused pytest, full-project Pyrefly, and the relevant broader suites described
in `references/local-agent-validation.md`.

### Runtime/gate coverage that remains mandatory

Where applicable to the final changed surface, preserve the established EPIC
gate coverage rather than substituting a small green subset. This includes
migration forward/fresh/rollback behavior; exact concurrency invariants;
checkpoint restart/resume and concurrent finish/resume; curated/autonomous
runs; empty-provider normalization; bounded extraction failures; truthful
batch summaries; authoritative synthesis/evidence/hash/validation; verifier
zero-object inconclusive behavior; doctor integrity/orphan/connectivity
separation; offline late diagnostics; PostgreSQL/Qdrant reconciliation; and
secret scanning.

All reset-authorized PostgreSQL/Qdrant tests must use
`scripts/disposable-test-services` with a literal unique namespace. Persistent
personal services are prohibited validation targets.

### Local edit authority

The local agent may make narrowly prescribed mechanical lint/format repairs
when necessary. Any failure suggesting architectural, semantic, persistence,
authority, migration, or test-contract changes must be returned to Central as
raw evidence instead of weakening tests, assertions, validation, static gates,
provenance, or runtime policy.

## Central acceptance after local return

Local PASS is necessary but not sufficient. Central must re-fetch the PR and
verify:

1. the head SHA is unchanged from the local validated SHA;
2. the complete diff is reviewed at that exact SHA;
3. CI is green for that exact head and required-check policy is visible;
4. current-head Codex/reviewer suggestions are fully dispositioned;
5. no baseline/config/test weakening occurred; and
6. disposable-service teardown evidence is complete.

No merge, issue closure, draft-state transition, or EPIC gate closure is implied
by local validation alone.
