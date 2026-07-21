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
- `ExecutionModePolicy` maps each new run to exactly one semantic authority:
  host-agent input for `agent_led`, the configured local model for
  `autonomous_local`, or supplied fixtures for `deterministic_debug`.
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
record, while conflicting reuse is rejected. `append_run_transition` remains a
low-level migration/repair primitive and deliberately does not mutate
`research_runs`. Normal callers use `ResearchRunService`, which supplies the
Section 10 transition policy to `apply_run_transition`.

`apply_run_transition` locks the current run row, checks command replay before
revision validation, rejects stale revisions and semantic proposals, inserts
one event and one transition, and updates the authoritative run in the same
transaction. Concurrent commands against one revision therefore cannot both
commit. Terminal states reject ordinary transitions. Explicit cancellation is
allowed from nonterminal states; explicit reopen moves a terminal run to
`created`, increments the revision, records `reopened_from_revision`, and marks
prior valid semantic artifacts invalid without deleting their provenance.
Semantic proposals used for a transition must be valid, belong to the same
run, and carry a `run_revision` equal to the command's expected revision.

`SemanticCallService` creates a `running` call before model transport, then
finalizes it with prompt/schema/model provenance, sanitized attempt telemetry,
usage, latency, validation failures, and explicit fallback lineage. Parsed
outputs are stored as separate `semantic_artifacts` rows, including invalid
schema outputs; invalid JSON and timeouts remain queryable on the failed call
without fabricating an artifact. Persistence is fail-closed when a semantic
context is supplied: a model result is not returned as accepted until its call
and artifact transaction commits.

Host-agent proposals use `ingest_host_artifact`, which applies the same
deterministic schema validator and artifact persistence path. Their call rows
identify `provider=host-agent` but deliberately omit endpoint, model, prompt,
usage, and transport-attempt claims that did not occur. Credential-bearing
keys, bearer values, and sensitive URL parameters are redacted before request,
response, error, or artifact values are hashed and persisted.

`SemanticCallService.decide` is the mode-aware decision boundary. It requires
the caller's run revision, rejects stale decisions, and never falls through to
a different authority. A valid host-agent artifact in `agent_led` is persisted
without invoking an inner model. Autonomous-local model calls are stage- and
idempotency-key scoped so a failed stage can be retried independently with a
new attempt key. Deterministic fixtures use the same validation and provenance
path, make no transport claims, and record semantic coverage as `unassessed`.

Execution-mode changes use `ResearchRunService.change_execution_mode` and the
`run-mode-change` CLI command. They compare-and-swap the run revision, record
the requester, approver, reason, prior mode, next mode, and policy version in
one append-only `run.execution_mode_changed` event, then invalidate prior valid
semantic artifacts without deleting provenance. Terminal runs must be reopened
before changing mode. The host-facing service defaults to `agent_led`; the
standalone `run-start` CLI defaults to `autonomous_local`.

Legacy wrappers pass completed decisions through the Phase 1 adapter described
in `legacy-adapters.md`. Shadow mode appends an idempotent
`legacy_adapter_comparisons` row but does not create a workflow invocation,
append an event, transition a run, or increment its revision. Authoritative
mode records search and scrape wrapper invocations through the existing
repository boundary. Comparison rows are append-only and queryable with
`research-db legacy-comparisons`; they are operational evidence, not a second
source of run state.

The CLI exposes `run-status`, `run-mode-change`, `run-transition`, `run-finish`,
`run-cancel`, and `run-reopen` as machine-readable service adapters. Callers
proposing semantic or concurrent work should always supply
`--expected-revision` and a stable `--idempotency-key`. This phase does not
integrate later planning, acquisition, coverage, report, or audit services.

## Migration and repair

The migration is forward-only and additive. Before production, capture the
normal PostgreSQL/blob/Qdrant recovery boundary described in
`research-store-operations.md`. PostgreSQL applies the revision in one
transaction. If the process is interrupted, PostgreSQL rolls back the partial
DDL and leaves Alembic at `0005_run_lifecycle`; rerun `research-db migrate`.
If Alembic reports v6 but required objects are absent, do not hand-create them:
restore the pre-migration PostgreSQL backup and rerun the forward migration.

No migration is added by the run or semantic-call service. To repair an interrupted command,
read `run-status` and the event/transition ledgers first. Retry an uncertain
commit with the same idempotency key; use a new key only for a new command
against the reported current revision. Reopen is the supported recovery path
for intentional work after a terminal state. A semantic call left `running`
after process loss may be finalized as failed by forward repair or retried with
the same call idempotency key; never delete its attempt or artifact provenance.
Never edit append-only ledgers.

Rollback is restore-based because later workflow records may depend on the new
tables. Restoring PostgreSQL does not require changing blobs, Qdrant, or Valkey
when their corpus boundary was unchanged, but always verify the captured
boundary before resuming ingestion.
