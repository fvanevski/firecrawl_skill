# Refactor EPIC gate remediation and local-agent handoff contract

This ledger records the post-merge remediation required after the Refactor EPIC
gate was evaluated against authoritative `main` at:

```text
EPIC_BASE_MAIN_SHA=407c37dd08093ebe26103b1477df05bb100db073
REMEDIATION_BRANCH=gate/refactor-epic-compat-closure
CENTRAL_IMPLEMENTATION_CHECKPOINT=4ba91a76f6f054d944828ddc7df5183fd72c86d1
```

The final handoff SHA is intentionally not hard-coded here because this document
is itself part of the remediation commit history. Central must supply and
re-read the immutable PR head immediately before local execution. Any later
head movement invalidates previous CI, review, and local evidence.

The implementation dispositions below describe code/test/document state. They
do **not** by themselves authorize local handoff. Exact-head CI, review-state,
base-freshness, and complete-diff evidence remain Central pre-handoff gates.

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
| index job lifecycle | `uow.index_jobs` |
| document/passages/search | `uow.documents` |
| terminal decisions | `uow.terminal_decisions` |
| run-asset/corpus persistence | `uow.snapshots` |

### B3. Issue #217 retained one hidden dependency on `uow.link_run_asset`

The issue-#217 batch implementation legitimately retains its published
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

### B5. Broad PostgreSQL integration authority still invoked deleted direct UoW domain methods

After the production boundary was corrected, the exact-head broad suite exposed
17 failures, all in `tests/integration/test_research_store_integration.py` and
all caused by stale direct test calls such as `uow.start_run`,
`uow.record_research_spec`, `uow.record_semantic_call`, `uow.search_lexical`,
`uow.claim_jobs`, `uow.list_plan_queries`, and `uow.insert_synthesis_stage`.
Restoring production aliases to satisfy those tests would have reversed B1/B2.

**Resolved centrally at the test authority instead.** The integration suite now
uses the same named ownership model as production, including `uow.runs`,
`uow.semantic_calls`, `uow.documents`, `uow.snapshots`,
`uow.retrieval_events`, `uow.index_jobs`, `uow.search_responses`, and
`uow.synthesis_stages`. The retained direct UoW behavioral contracts remain
explicit exceptions: transaction/infrastructure operations, `persist_ingest`,
the issue-#217 ingestion-batch/export methods, and `get_trace`.

The mechanical migration was fail-closed: unknown direct UoW calls caused the
migration to abort rather than guessing an owner. The transformation was Ruff
normalized and its focused regression set passed before the migrated files were
committed. The one-shot migration helper was then removed from the branch.

## Important non-blocking findings and disposition

### N1. Ruff import-order/format findings in migrated ARC-17 and handoff tests

The repository-role migrations exposed `I001` import-order findings in
`tests/integration/test_arc17_corrective_defects.py` and
`tests/unit/test_handoff.py` before those suites could provide useful runtime
evidence.

**Resolved without suppression.** The affected authorities were normalized with
Ruff import fixing/formatting; no `noqa`, lint exclusion, assertion weakening,
or test removal was introduced. The same normalization pass covered the broad
integration authority and repository-boundary contract.

### N2. Test doubles could silently recreate the deleted generic UoW shape

Report, handoff, and ARC-17 fixtures had enough direct-method mocking to obscure
whether production callers still obeyed repository ownership.

**Resolved.** Report fixtures use `synthesis_stages`/`evidence_packets`; the
handoff UoW double exposes explicit `evidence_packets`, `runs`, and `coverage`
roles; ARC-17 mocks/PostgreSQL helpers use `synthesis_stages` and
`evidence_packets`. The fixtures preserve their behavioral assertions without
recreating production compatibility aliases.

### Inherited architecture constraints

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

### PR #296 current-head review discipline

PR #296 is the active remediation PR. At the intermediate exact head
`07b8413be3fa6a65722eb3aaad28b52b0a6676a4`, Central's complete review-state
query reported no submitted reviews, no unresolved review threads, and no
requested reviewers. That observation is historical evidence only; it is not a
claim about a later head.

Immediately before local handoff, Central must query the **final immutable PR
head** again and inspect all returned reviews, review threads, requested
reviewers, and other available review-state evidence. Every current-head
Codex-authored suggestion must be dispositioned as either:

- **implemented**, with the exact file/symbol and regression evidence; or
- **rejected**, with a specific architectural/evidentiary reason.

Zero final-head Codex suggestions is a valid disposition only when the complete
available exact-head review surface was inspected. Historical or inferred
review text must never be substituted for absent review evidence.

## Test and documentary gaps closed centrally

`tests/contract/test_issue_269_uow_repository_boundary.py` now provides both a
production architecture guard and a critical-test-authority guard. It:

- scans production ASTs for direct `uow.<domain operation>()` calls and permits
  only transaction infrastructure plus the documented `persist_ingest` and
  issue-#217 class APIs;
- rejects cross-domain acquisition/candidate/extraction/semantic calls through
  `uow.runs`;
- rejects resurrection of the generic compatibility-router implementation;
- verifies the corrected repository protocol inheritance and UnitOfWork role
  annotations;
- verifies issue-#217 run-asset linkage uses `uow.snapshots`; and
- scans the broad PostgreSQL integration suite, ARC-17 corrective suite,
  handoff fixtures, and report fixtures for stale direct `uow.<domain>()`
  calls, allowing only the explicit direct-UoW contract set.

The older #259 integration test was migrated to the same final-state invariant.
Package documentation in `research_store.__init__` now describes the retained
class APIs accurately: they remain directly callable behavioral contracts and
are not entered-UoW instance delegates.

A dedicated read-only GitHub Actions gate, stored at
`.github/workflows/central-test-authority-migration.yml` and displayed as
**UoW test authority boundary**, now runs Ruff/format checks on the critical test
authorities and executes the repository-boundary, handoff, and report focused
regressions when the relevant source/test surface changes. Its purpose is
regression enforcement; it has no write permission and contains no migration or
self-modifying behavior.

The temporary use of CI as a bounded transformation surface has been removed.
`.github/workflows/ci.yml` was restored byte-for-byte to its pre-bootstrap blob
(`8699e30d59f7a2ad89664a3821d34fdb1eafd58a`), and the one-shot migration helper
was deleted. Thus no temporary write-enabled remediation mechanism remains in
the final implementation surface.

This ledger supplements, and does not weaken,
`references/issue-269-final-cleanup.md` and
`references/local-agent-validation.md`.

## Central pre-handoff gate

Implementation completion is not handoff authorization. Before Central emits a
local-agent SHA it must, against one unchanged final PR head:

1. re-fetch PR head and authoritative `main` base;
2. inspect the complete changed-file/diff surface for that head;
3. require broad Python 3.11/3.12 CI, Ruff, Pyrefly, repository-boundary,
   authoritative-storage/Qdrant, ARC-17, acquisition/retrieval/reporting, and
   other triggered exact-head workflows to complete successfully;
4. inspect current-head Codex/reviewer state and disposition every returned
   suggestion/thread;
5. inspect merge requirements/base freshness without interpreting missing policy
   visibility as no requirement; and
6. verify no test, assertion, validation, provenance, static gate, or authority
   check was weakened to obtain green evidence.

Any head movement after those observations invalidates them and requires the
pre-handoff gate to be repeated.

## Local-agent handoff: execute only after Central says the PR head is frozen

The local agent is an execution/diagnostic authority for host-dependent checks,
not the architectural decision-maker. It must not redesign production behavior
in response to a failing test.

### Tool contract

- **Serena:** first-line semantic navigation, symbol/reference census,
  dependency analysis, diagnostics, and structural inspection. Use no-memories;
  inspect before any mechanical edit and audit after it.
- **Probe:** supplemental extraction/search only when it adds coverage without
  duplicating Serena; it is not an authority source for mutable repository
  state.
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
BASE_SHA=<authoritative main/base SHA re-read at handoff>
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
tests/integration/test_research_store_integration.py
tests/integration/test_arc17_corrective_defects.py
tests/unit/test_handoff.py
tests/unit/test_report_service.py
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
