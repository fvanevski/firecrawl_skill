# Workflow State Schema

Alembic revision `0006_workflow_state` establishes the PostgreSQL workflow foundation. PostgreSQL is the sole authority for run, invocation, event, semantic, evidence, and audit state. Scratch files are disposable diagnostics and are never read to resolve current state.

## Authority and invariants

- `research_runs.state` is the authoritative lifecycle state.
- `research_runs.lifecycle_revision` is a monotonic compare-and-swap revision.
- `research_run_transitions` and `research_events` are append-only at the database layer.
- Every persistent wrapper operation has an authoritative `research_invocations` row.
- Idempotency keys are scoped to their run or invocation and reject conflicting reuse.
- Qdrant, Valkey, blobs, and scratch output cannot advance or overwrite workflow state.
- Terminal runs reject new operations until explicitly reopened.

## Data dictionary

| Table | Purpose | Primary invariants |
|---|---|---|
| `research_runs` | Authoritative current state for one research run | State matrix; monotonic revision; immutable execution-mode provenance; current spec/budget/coverage pointers |
| `research_run_transitions` | Immutable state-transition ledger | Unique run revision and idempotency key; prior and next states differ |
| `research_invocations` | Search, scrape, retrieval, synthesis, audit, and child operations | Same-run parent; unique external ID; start revision; terminal status and output |
| `research_events` | Immutable ordered operational event stream | Same-run invocation FK; stable sequence/cursor order; unique run idempotency key |
| `research_specs` | Immutable versioned ResearchSpec records | Unique spec revision; canonical payload hash; validation result |
| `semantic_calls` | Model or host-agent decision provenance | Same-run invocation; prompt/model/input identity; explicit mechanical status |
| `semantic_artifacts` | Validated structured semantic outputs | Same-run call; schema identity; canonical content hash; validation status |
| `budget_snapshots` | Immutable resource authorization | Run/spec revision binding; policy/config hashes; effective hard caps |
| `research_run_assets` | Run-to-snapshot provenance | Explicit role; no inferred filesystem membership |
| `coverage_snapshots` | Versioned coverage state | Monotonic coverage revision; deterministic ledger |
| `audit_assessments` / `audit_stage_outputs` | Staged audit evidence and results | Immutable identity, explicit model/policy provenance, partial-stage retention |

## State machine

Permitted transitions are defined in `research_store.run_service.PERMITTED_TRANSITIONS`:

```text
created → planning
planning → corpus_review | failed
corpus_review → acquiring | retrieving | failed
acquiring → extracting | coverage_review | partial | failed
extracting → indexing | coverage_review | failed
indexing → coverage_review | partial | failed
coverage_review → acquiring | extracting | retrieving | synthesizing | partial | failed
retrieving → coverage_review | synthesizing | failed
synthesizing → validating | failed
validating → completed | partial | failed
```

`cancelled` is available through the explicit cancellation command from any nonterminal state. `completed`, `partial`, `failed`, and `cancelled` are terminal.

## Wrapper workflow boundary

`fsearch` and `fscrape` call the PostgreSQL-only `WorkflowOperationService` through internal CLI commands:

```text
run-operation-start
run-operation-finish
```

The start boundary:

1. resolves the `fr_<uuid>` run;
2. rejects terminal or incompatible state;
3. advances only permitted acquisition stages;
4. records a running `research_invocations` row and event before network work.

The finish boundary:

1. resolves and validates the invocation/run binding;
2. records the terminal invocation exactly once;
3. advances to `indexing` only when `_corpus.json` reports committed assets;
4. remains retry-safe when the invocation commit succeeded but a later transition was interrupted.

`frun finish` verifies run-scoped indexing and advances the permitted terminal path. It does not jump directly from `created` to `completed`.

## Repository operations

`PostgresUnitOfWork` provides bounded record methods. `ResearchRunService` applies lifecycle policy and compare-and-swap revisions. `InvocationService` manages authoritative invocation state. `WorkflowOperationService` coordinates wrapper boundaries without adding a second source of truth.

Execution-mode changes record requester, approver, reason, prior mode, next mode, and policy version, then invalidate affected semantic artifacts without deleting provenance.

## Repair

After an uncertain command:

1. read `research-db run-status <fr_id>`;
2. read the invocation/event ledger;
3. retry the same command with the same idempotency key;
4. use a new key only for a genuinely new command against the reported revision.

Never edit append-only ledgers. Reopen is the supported path for intentional work after terminal state.

The clean schema head is `0038_postgres_authority`. Databases created by the removed deprecated migration path must be reset with `scripts/reset-firecrawl-research` before use.
