# Phase 1 gate report

Issue: `#11` (`P1-GATE`)  
Review date: 2026-07-21  
Decision: **approved**

All Phase 1 P0 dependencies are closed as completed: `#2`, `#4`, `#5`,
`#6`, `#7`, `#8`, `#9`, and `#10`. No dependency or acceptance blocker was
found during the gate review.

## Exit criteria

| Criterion | Evidence | Result |
| --- | --- | --- |
| A run can be created in `agent_led` or `autonomous_local` mode. | `test_phase1_gate_run_spec_and_transactional_rejection`, `test_standalone_cli_defaults_to_autonomous_local_mode`, and execution-mode policy tests | Pass |
| A ResearchSpec can be proposed, validated, persisted, and versioned. | The gate integration test submits a host-agent proposal through `SemanticCallService`, validates it through the domain registry, persists revisions 1 and 2, and verifies the run points to revision 2. Domain fixture, schema, round-trip, reference, and stale-revision tests cover rejection behavior. | Pass |
| Invalid lifecycle transitions are rejected transactionally. | The gate integration test attempts `created -> completed` and verifies no state, revision, transition, or non-creation event mutation. Transition-matrix, concurrent transition, stale-revision, terminal-state, idempotency, and append-only-ledger tests provide the broader coverage. | Pass |
| Legacy entry points can invoke the new services through adapters. | Compatibility, shadow, authoritative-routing, divergence-query, failure-propagation, and wrapper configuration tests in `test_research_store.py`, `test_research_store_integration.py`, and `test_workflow.py` | Pass |

## Test evidence

The deterministic Phase 1 suite was run without network access:

```bash
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider \
  scripts/test_classifier.py \
  scripts/test_workflow.py \
  scripts/test_golden_fixtures.py \
  scripts/test_research_domain.py \
  scripts/test_budget_policy.py \
  scripts/test_research_store.py \
  scripts/test_index_runtime.py
```

Result: 176 passed, 1 strict expected failure. The expected failure is the
documented legacy Qdrant-outage degradation-reporting defect assigned to later
FR-013 work; it is not normalized as correct behavior and is not a Phase 1 P0
waiver.

The PostgreSQL suite was run against the explicitly named disposable database
`research_assets_test_codex` using the repository's research-store virtual
environment:

```bash
RESEARCH_STORE_TEST_DATABASE_URL='<disposable-test-dsn>' \
RESEARCH_STORE_TEST_ALLOW_RESET=research_assets_test_codex \
PYTHONDONTWRITEBYTECODE=1 \
.venv-research-store/bin/pytest -q -p no:cacheprovider \
  scripts/test_research_store_integration.py
```

Result after adding the gate test: 26 passed. This includes fresh and populated
Alembic migration paths, interrupted-migration forward repair, constraints,
concurrent writes, semantic-call failure provenance, adapter behavior,
snapshot versioning, exact manifest binding, and lease-token rejection.

Shell entry points also pass `bash -n` for `fsearch`, `fscrape`,
`fsearch_smart`, `frun`, and `research-db`.

## Manual architecture-invariant review

The review covered `research-store-architecture.md`,
`workflow-state-schema.md`, `legacy-adapters.md`, Alembic revisions `0001`
through `0008`, the PostgreSQL unit of work, run and semantic services, adapter
boundary, and relevant tests. The following invariants remain intact:

- PostgreSQL is authoritative for corpus and workflow state. Qdrant is a
  rebuildable projection, Valkey is best-effort, and scratch/catalog artifacts
  are compatibility or diagnostic exports.
- Source bytes are immutable content-addressed snapshots. Parser,
  normalization, and chunker changes create versioned derivations rather than
  false snapshots.
- Run state uses a locked compare-and-swap lifecycle revision. Successful
  transitions write exactly one transition and event in the same transaction;
  transition, event, and comparison ledgers remain append-only.
- Embedding jobs remain bound to the exact manifest and index definition.
  Workers must hold the current lease token to renew or finish a job.
- Semantic artifacts, failed attempts, retrieval choices, batches, and
  compatibility exports retain explicit provenance. Reopen and mode changes
  invalidate stale artifacts without deleting their history.
- Legacy compatibility remains the default. Shadow proposals do not mutate
  authoritative workflow state, and adapter failures do not silently fall back.

No Phase 2 planning, acquisition, coverage, extraction, evidence, synthesis,
or audit service was introduced by this gate.

## Completion report

- **Files changed:** the Phase 1 gate report, the PostgreSQL gate acceptance
  test, and the README validation pointer.
- **Schema or migration changes:** none.
- **Before/after behavior:** runtime behavior is unchanged. The repository now
  has one end-to-end gate test and a durable approval record tying the Phase 1
  criteria to executable evidence.
- **Failure paths tested:** invalid and stale lifecycle transitions,
  conflicting/idempotent writes, append-only mutation attempts, invalid domain
  data and references, semantic invalid JSON/schema/timeout/fallback paths,
  persistence loss, Qdrant loss, Valkey loss, model loss, stale leases, exact
  manifest mismatch, adapter misconfiguration, and shadow no-side-effect
  behavior.
- **Compatibility impact:** none. No CLI flag, default adapter mode, schema,
  migration, persistence contract, or output format changed.
- **Waived non-P0 defects:** none. The existing strict FR-013 xfail remains a
  documented later-phase defect rather than a waiver.
- **Unresolved risks:** live production-service behavior is not inferred from
  this destructive disposable-database suite. Production cutover still
  requires the separate recorded Qdrant/Valkey/wrapper acceptance campaign
  described in `research-store-operations.md`.
- **Rollback or forward repair:** revert the gate-only commit to remove the
  test and report; no data rollback is required. For an interrupted workflow
  command, inspect status and append-only ledgers, then retry with the same
  idempotency key. For migration recovery, restore the PostgreSQL/blob recovery
  boundary if necessary and rerun the forward migration; never rewrite ledgers
  or reconstruct authoritative state from Qdrant, Valkey, or scratch exports.
