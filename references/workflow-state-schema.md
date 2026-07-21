# Workflow State Schema

Alembic revision `0006_workflow_state` adds the Phase 1 PostgreSQL workflow
foundation. PostgreSQL is authoritative for these records; filesystem Catalog
and scratch records remain compatibility and diagnostic exports.

## Authority and compatibility

- `research_runs.state` is the authoritative lifecycle state and
  `lifecycle_revision` is its monotonic compare-and-swap revision.
- Existing `status`, `original_request`, and `outcome` columns remain as legacy
  compatibility projections. Existing rows are retained and backfilled without
  changing corpus, snapshot, derivation, index, job, or provenance records.
- Existing rows are marked with `execution_mode=legacy` because their original
  semantic authority cannot be inferred safely. New repository-created rows
  default to `agent_led` unless the caller supplies another supported mode.
- `research_run_transitions` and `research_events` reject `UPDATE` and `DELETE`
  at the database layer. Corrections are new ledger rows, never rewrites.
- Idempotency keys are scoped to their owning run or semantic call. Reuse with
  the same immutable mutation returns the original identity; conflicting reuse
  is rejected.

## Data dictionary

| Table | Purpose | Identity and invariants |
| --- | --- | --- |
| `research_runs` | One authoritative current state per research run | `state`; monotonic `lifecycle_revision`; current immutable spec pointer; non-negative coverage revision |
| `research_run_transitions` | Immutable state-transition ledger | Unique `(run_id, lifecycle_revision)` and `(run_id, idempotency_key)`; prior and next states differ |
| `research_invocations` | Top-level and child search, scrape, retrieval, synthesis, or audit operations | Same-run parent FK; unique run idempotency key; lifecycle revision captured at creation |
| `research_events` | Immutable operational event stream | Same-run invocation FK; unique run idempotency key; stable `(created_at, id)` cursor order |
| `research_specs` | Immutable, versioned structured research specifications | Unique run spec revision and idempotency key; canonical payload SHA-256; validation result |
| `semantic_calls` | Model/transport provenance for one semantic decision call | Same-run invocation FK; prompt/model/input hash; mechanical call status; unique run idempotency key |
| `semantic_artifacts` | Validated structured output of one semantic call | Same-run call FK; schema name/version; canonical payload SHA-256; unique call idempotency key |
| `compatibility_exports` | Regenerable Catalog or scratch-compatible export record | Run and optional same-run invocation/event cursor; source-state hash; database revision or event cursor; export failure separate from workflow state |

JSON payload hashes use UTF-8 JSON serialized with sorted keys and compact
separators. Hashes identify exact persisted structured content; they do not
replace schema validation.

## Repository operations

`PostgresUnitOfWork` exposes bounded, idempotent record methods for every v6
table. Concurrent retries with one idempotency key converge on one stored
record, while conflicting reuse is rejected. `append_run_transition` is a
low-level ledger write and deliberately does not mutate `research_runs` or
enforce the Section 10 transition matrix.

The v6 migration does not implement `ResearchRunService`, permitted-transition
policy, stale-proposal rejection, reopen behavior, atomic state/event mutation,
or CLI status routing; those belong to issue #7. It also does not integrate the
model gateway or implement later Phase 1 search-plan, candidate, coverage,
claim, report, or audit tables.

## Migration and repair

The migration is forward-only and additive. Before production, capture the
normal PostgreSQL/blob/Qdrant recovery boundary described in
`research-store-operations.md`. PostgreSQL applies the revision in one
transaction. If the process is interrupted, PostgreSQL rolls back the partial
DDL and leaves Alembic at `0005_run_lifecycle`; rerun `research-db migrate`.
If Alembic reports v6 but required objects are absent, do not hand-create them:
restore the pre-migration PostgreSQL backup and rerun the forward migration.

Rollback is restore-based because later workflow records may depend on the new
tables. Restoring PostgreSQL does not require changing blobs, Qdrant, or Valkey
when their corpus boundary was unchanged, but always verify the captured
boundary before resuming ingestion.
