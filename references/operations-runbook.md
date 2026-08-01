<!-- @format -->

# Operations Runbook

Complete runbook for deploying, operating, debugging, benchmarking, and recovering the Firecrawl Research Skill. PostgreSQL is the sole workflow and corpus authority. Blob storage holds immutable payload bytes, Qdrant is a rebuildable retrieval projection, Valkey is transient coordination, and scratch files are disposable diagnostics.

## Table of contents

1. [Architecture overview](#1-architecture-overview)
2. [Service boundaries](#2-service-boundaries)
3. [Execution modes](#3-execution-modes)
4. [Deployment](#4-deployment)
5. [Configuration variables](#5-configuration-variables)
6. [Backup and restore](#6-backup-and-restore)
7. [Qdrant rebuild](#7-qdrant-rebuild)
8. [Valkey loss handling](#8-valkey-loss-handling)
9. [Endpoint restart](#9-endpoint-restart)
10. [Interrupted-run recovery](#10-interrupted-run-recovery)
11. [PostgreSQL workflow recovery](#11-postgresql-workflow-recovery)
12. [Benchmarking](#12-benchmarking)
13. [Release evidence](#13-release-evidence)
14. [Destructive commands](#14-destructive-commands)
15. [Recovery drill checklist](#15-recovery-drill-checklist)

---

## 1. Architecture overview

| Layer | Component | Role | Recovery rule |
| --- | --- | --- | --- |
| **Authoritative state** | PostgreSQL | Runs, invocations, events, corpus, budgets, evidence, audits, jobs | Restore first; never infer state from another layer |
| **Immutable payloads** | Content-addressed blob root | Raw source bytes referenced by snapshots | Restore with PostgreSQL at the same boundary |
| **Retrieval projection** | Qdrant | Fingerprinted dense-vector collections | Rebuild from PostgreSQL chunks |
| **Transient coordination** | Valkey | Wakeups and bounded cache | Loss is safe; workers poll PostgreSQL |
| **Diagnostics** | Scratch directories | Human-readable acquisition output and identity reports | Delete freely; never read as authority |

Governing rules:

1. PostgreSQL is the only workflow and metadata authority.
2. Blob bytes are immutable and content-addressed.
3. Qdrant is rebuilt, reconciled, and switched through a stable alias.
4. Valkey never owns durable work.
5. Scratch output is diagnostic only.
6. Run transitions are compare-and-swap mutations with immutable ledgers.
7. Wrappers validate the run before network acquisition when persistence is enabled.
8. Enabled persistence is fail-closed.
9. Model artifacts are schema-validated before acceptance.
10. No remote fallback occurs without explicit configuration.

## 2. Service boundaries

### PostgreSQL

Authoritative for:

- `research_runs`, transitions, events, and invocations;
- sources, snapshots, documents, blocks, chunks, and run-asset links;
- ResearchSpec, budgets, search plans, candidates, coverage, claims, and evidence;
- semantic calls, artifacts, audits, terminal decisions, telemetry, and cache metadata;
- index definitions, embedding manifests, jobs, leases, and worker heartbeats.

Never hand-edit append-only ledgers. Use service commands with the current lifecycle revision and a stable idempotency key.

### Blob root

The blob root contains immutable payload bytes identified by SHA-256. PostgreSQL snapshot rows reference these hashes. A file without a row is an orphan; a row without a valid file is corruption. Run `verify-blobs` and `doctor` after restore.

### Qdrant

Qdrant contains only the dense-retrieval projection. Physical collections are fingerprinted; `research_chunks_active` is the stable query alias. Collection contents cannot recreate run, corpus, or provenance state.

### Valkey

Valkey provides wakeups and bounded cache entries. Workers also poll PostgreSQL, so lost messages do not strand jobs. Clearing Valkey must not alter run or corpus truth.

### Local model endpoints

Embedding and reranking endpoints must match the configured identity and dimension. Generative endpoints are used only in explicit semantic stages. Endpoint unavailability remains visible; it is not silently replaced.

## 3. Execution modes

### `agent_led`

A host agent supplies semantic artifacts. The system validates and persists them but does not claim a local model call occurred.

### `autonomous_local`

The configured local model is semantic authority. Model, revision, prompt version, request hash, response metadata, and validated artifacts are persisted.

### `deterministic_debug`

Fixtures drive semantic decisions without network model calls. This mode is for reproducible tests and diagnostics, not production evidence claims.

Change mode only through `run-mode-change`, with current revision, requester, approver, and reason.

## 4. Deployment

### 4.1 Prerequisites

- PostgreSQL 16 or compatible current server;
- Qdrant compatible with the pinned client;
- Valkey with authenticated access;
- Python environment from `requirements-research-store.txt`;
- Firecrawl Node CLI for `fsearch` and `fscrape`;
- configured embedding and reranker endpoints;
- shared Docker network `agent-search` for the supplied container layouts.

### 4.2 Environment setup

```bash
cd "<skill-root>"
export FIRECRAWL_RESEARCH_PERSIST=on
export FIRECRAWL_RESEARCH_PYTHON="<skill-root>/.venv-research-store/bin/python"
source scripts/research-env
```

Explicit environment variables take precedence over the repository-root `.env`. Never print resolved secrets.

### 4.3 Clean database initialization

This PostgreSQL-only baseline intentionally requires a clean store. Existing databases from an earlier schema line are not upgraded.

```bash
scripts/reset-firecrawl-research
scripts/research-db status
scripts/research-db ingest-ready
scripts/research-db doctor
```

Expected schema head:

```text
0038_postgres_authority
```

The reset command is destructive and must display guarded PostgreSQL, Qdrant, Valkey, and blob targets before deletion.

### 4.4 Worker service

Production should run the persistent index worker through `firecrawl-research-indexer.service`.

```bash
systemctl --user daemon-reload
systemctl --user enable --now firecrawl-research-indexer.service
systemctl --user --no-pager --full status firecrawl-research-indexer.service
```

Equivalent foreground command:

```bash
scripts/research-db worker \
  --batch-size 32 \
  --poll-seconds 5 \
  --lease-seconds 300 \
  --max-attempts 5
```

### 4.5 Persistence modes

| Mode | Behavior |
| --- | --- |
| `on` | Requires a healthy store and a valid research run before persistent acquisition; fails closed |
| `auto` | Persists when `DATABASE_URL` resolves; otherwise permits scratch-only acquisition |
| `off` | Produces scratch output only; no database, blob, job, or Qdrant writes |

Private scratch-only acquisition:

```bash
FIRECRAWL_RESEARCH_PERSIST=off \
  scripts/fscrape 'https://example.com/private'
```

## 5. Configuration variables

### 5.1 Core persistence

| Variable | Purpose |
| --- | --- |
| `FIRECRAWL_RESEARCH_PERSIST` | `on`, `auto`, or `off` persistence policy |
| `FIRECRAWL_RESEARCH_PYTHON` | Python executable used by `research-db` |
| `DATABASE_URL` | PostgreSQL connection URL |
| `BLOB_ROOT` | Content-addressed blob directory |
| `FIRECRAWL_RESEARCH_RUN_ID` | Default PostgreSQL `fr_<uuid>` for wrappers |

### 5.2 Vector retrieval

| Variable | Purpose |
| --- | --- |
| `QDRANT_URL` | Qdrant endpoint |
| `QDRANT_API_KEY` | Qdrant credential |
| `QDRANT_ALIAS` | Stable active alias, normally `research_chunks_active` |
| `EMBEDDING_URL` | OpenAI-compatible embedding endpoint |
| `EMBEDDING_API_KEY` | Embedding endpoint credential |
| `EMBEDDING_MODEL` | Model name sent to the endpoint |
| `EMBEDDING_REVISION` | Immutable model/revision identity |
| `EMBEDDING_DIMENSION` | Expected vector dimension |
| `RERANKER_URL` | Reranker endpoint |
| `RERANKER_API_KEY` | Reranker credential |
| `RERANKER_MODEL` | Reranker model identity |
| `RERANKER_CANDIDATE_LIMIT` | Maximum reranking candidate count |

### 5.3 Transient coordination

| Variable | Purpose |
| --- | --- |
| `VALKEY_URL` | Authenticated Valkey URL |

### 5.4 Model endpoints

| Variable | Purpose |
| --- | --- |
| `FIRECRAWL_LLM_LOCAL_BASE_URL` | Local OpenAI-compatible generative endpoint |
| `FIRECRAWL_LLM_LOCAL_MODEL` | Local model name |
| `FIRECRAWL_AUDIT_AUTO_SEMANTIC` | Enable automatic semantic audit stages |

### 5.5 Derivation versions

| Variable | Purpose |
| --- | --- |
| `PARSER_VERSION` | Active parser identity |
| `NORMALIZATION_VERSION` | Active normalization identity |
| `CHUNKER_VERSION` | Active chunker identity |
| `TOKENIZER_NAME` | Tokenizer used for bounded passages and budgets |

## 6. Backup and restore

### 6.1 Backup procedure

Capture PostgreSQL and blobs at one recovery boundary:

```bash
systemctl --user stop firecrawl-research-indexer.service
pg_dump --format=custom --file=research.pg.dump "$DATABASE_URL"
find "$BLOB_ROOT" -type f -print0 | sort -z | xargs -0 sha256sum > blob-inventory.sha256
scripts/research-db status > status.json
scripts/research-db doctor > doctor.json
scripts/research-db index-list > indexes.json
systemctl --user start firecrawl-research-indexer.service
```

Qdrant snapshots are optional acceleration, not authority. Valkey does not require backup.

### 6.2 Restore procedure

1. Stop writers and the index worker.
2. Restore PostgreSQL.
3. Restore the matching blob root.
4. Verify hashes and schema.
5. Rebuild the current Qdrant collection.
6. Drain jobs, reconcile points, activate the index.
7. Restart the worker and run `doctor`.

```bash
scripts/research-db verify-blobs
scripts/research-db status
scripts/research-db index-build --current-config --all
scripts/research-db worker --once --batch-size 64
scripts/research-db reconcile-qdrant
scripts/research-db index-activate '<index-id>'
scripts/research-db doctor
```

### 6.3 Failed rollout rollback

Restore PostgreSQL and blobs from the captured boundary, then rebuild or switch Qdrant. Do not attempt to reconstruct database state from scratch output or vectors.

## 7. Qdrant rebuild

### 7.1 Build a new index

```bash
scripts/research-db index-list
scripts/research-db index-build --current-config --all
scripts/research-db worker --once --batch-size 64
```

### 7.2 Verify before activation

```bash
scripts/research-db reconcile-qdrant
scripts/research-db doctor
```

Activation requires compatible schema, complete manifests, zero missing/orphaned points, and a successful probe.

### 7.3 Activate

```bash
scripts/research-db index-activate '<index-id>'
```

### 7.4 Rollback

```bash
scripts/research-db index-rollback '<prior-index-id>'
```

### 7.5 Prune old collections

```bash
scripts/research-db index-prune --dry-run
scripts/research-db index-prune --dry-run --keep-last 2
scripts/research-db index-prune --force --index-id '<exact-index-id>'
```

Never prune the active index. Review the dry run and specify an exact target.

### 7.6 Interrupted cutover recovery

Read `index-list`, `reconcile-qdrant`, and `doctor`. Retry activation with the same target. If the alias points to a verified prior collection, retain it until the new activation finalizes.

## 8. Valkey loss handling

### 8.1 Why it is safe

Job and run truth lives in PostgreSQL. The worker alternates bounded Valkey waits with PostgreSQL polling.

### 8.2 Recovery

```bash
# Restart or recreate Valkey, then verify:
scripts/research-db doctor
systemctl --user restart firecrawl-research-indexer.service
```

No database repair is required solely because Valkey was lost.

### 8.3 When to investigate

Investigate repeated authentication errors, cache poisoning, unbounded memory growth, or worker latency that persists despite healthy PostgreSQL polling.

## 9. Endpoint restart

### 9.1 Embedding or reranker restart

1. Stop the worker if the endpoint will be unavailable for an extended interval.
2. Restart the endpoint with the same model identity.
3. Verify `endpoint-health` and `doctor`.
4. Restart the worker and allow retryable jobs to drain.

```bash
scripts/research-db endpoint-health
scripts/research-db resource-status
scripts/research-db doctor
```

A model/revision/dimension change requires a new index definition and collection.

### 9.2 Qdrant restart

Restart Qdrant, then run `reconcile-qdrant`. Rebuild if the collection or points are missing.

### 9.3 PostgreSQL restart

Stop writers, restart PostgreSQL, verify `status` and `ingest-ready`, then restart the worker. Stale leases are reclaimed through normal lease policy.

### 9.4 Complete endpoint outage

Recover PostgreSQL first, then blobs, Qdrant, Valkey, and model endpoints. Resume work only after `doctor` reports a current schema and healthy authority boundary.

## 10. Interrupted-run recovery

### 10.1 State machine basics

Normal successful path:

```text
created -> planning -> corpus_review -> acquiring
-> extracting -> indexing -> coverage_review
-> synthesizing -> validating -> completed
```

`WorkflowOperationService` advances acquisition boundaries idempotently. `frun finish` performs the final indexed-asset check and permitted terminal progression.

### 10.2 Diagnose

```bash
scripts/research-db run-status 'fr_<uuid>'
scripts/research-db doctor
```

Record the current state and lifecycle revision before acting.

### 10.3 Forward repair

Retry an uncertain command with the same idempotency key. If the revision changed, inspect status before issuing a genuinely new command.

```bash
scripts/research-db run-transition 'fr_<uuid>' planning \
  --expected-revision 0 \
  --idempotency-key 'stable-command-id'
```

### 10.4 Reopen a terminal run

```bash
scripts/frun reopen 'fr_<uuid>' --reason 'additional evidence required'
```

Reopen records a new revision and invalidates stale semantic artifacts without deleting provenance.

### 10.5 Cancel a run

```bash
scripts/frun cancel 'fr_<uuid>' --reason 'operator request'
```

### 10.6 Semantic call recovery

Do not delete a failed or interrupted call. Finalize it as failed or retry the stage with the documented idempotency key and current revision.

## 11. PostgreSQL workflow recovery

### 11.1 Wrapper preflight

Persistent wrappers require an existing nonterminal run. They call the internal boundary before network work:

```bash
scripts/research-db run-operation-start \
  'fr_<uuid>' 'fc_<uuid>' extraction.batch \
  --input-json '{"urls":["https://example.com"]}'
```

Normal operators use `fscrape` or `fsearch`; the internal command exists for testing and recovery.

### 11.2 Wrapper completion

After corpus persistence, wrappers report the `_corpus.json` diagnostic manifest to the boundary:

```bash
scripts/research-db run-operation-finish \
  'fr_<uuid>' 'fc_<uuid>' \
  --status complete \
  --corpus-manifest '/tmp/firecrawl_scratch/fc_<uuid>/scrape/_corpus.json'
```

The manifest supplies committed identities only. PostgreSQL remains the source of truth.

### 11.3 Failed operation

A wrapper trap records failure against the existing invocation. Retry the operation with a new external invocation ID; do not reuse a terminal invocation ID for different input.

### 11.4 Scratch import and JSON diagnostics

```bash
scripts/research-db import-scratch '/tmp/firecrawl_scratch/fc_<uuid>' --dry-run
scripts/research-db import-scratch '/tmp/firecrawl_scratch/fc_<uuid>'
scripts/research-db export-invocation 'fc_<uuid>' --output invocation.json
scripts/research-db export-run 'fr_<uuid>' --output run.json
```

These are explicit one-time operations. JSON output is not consumed as workflow authority.

## 12. Benchmarking

### 12.1 Run a benchmark campaign

```bash
scripts/research-db benchmark run \
  --dataset tests/fixtures/benchmark/benchmark-v2.json \
  --output benchmark-results.json
```

Exit code `0` means `go` or `go_with_conditions`; exit code `2` means `no_go`. Read the JSON outcome.

### 12.2 View results

```bash
scripts/research-db benchmark results --results-path benchmark-results.json
```

### 12.3 Generate a report

```bash
scripts/research-db benchmark report \
  --results-path benchmark-results.json \
  --output benchmark-report.md
```

### 12.4 Benchmark requirements

Use fixed versioned inputs, explicit execution mode, measured telemetry, and retained environment metadata. Never label placeholder or simulated metrics as production evidence.

## 13. Release evidence

### 13.1 Manifest contents

Exact-head evidence binds candidate SHA, workflow run, required job conclusions, environment identity, artifact digest, and source state.

### 13.2 Required CI jobs

- release invariants on Python 3.11 and 3.12;
- full tests on Python 3.11 and 3.12;
- strict campaign contract tests;
- Ruff lint and formatting;
- exact-head evidence generation for release candidates.

### 13.3 Candidate discipline

Any code or configuration change after validation invalidates the evidence. Generate evidence only from the exact candidate SHA and never move an approved release tag.

### 13.4 Artifact store

Retain the evidence manifest and workflow artifact digest. Do not substitute local unrecorded output for the CI artifact.

## 14. Destructive commands

### 14.1 `reset-firecrawl-research`

| Field | Requirement |
| --- | --- |
| Scope | PostgreSQL volume, Valkey volume, Qdrant data/snapshots, blob corpus |
| Guard | Clean `main`, exact path validation, stopped workers, typed `RESET` unless `--yes` |
| Recovery | None without an external backup |
| Use | Clean initialization only; never routine repair |

### 14.2 `index-prune --force`

Review `--dry-run`, confirm the exact inactive index ID, retain rollback coverage, then force only that target.

### 14.3 Blob deletion

`verify-blobs` is read-only. Do not manually delete referenced hashes. Orphan cleanup must be separately bounded, reviewed, and performed only after a matching PostgreSQL backup.

### 14.4 Manual database editing

Direct updates or deletes to runs, transitions, events, invocations, manifests, or jobs are unsupported. Repair through service commands or restore from backup.

### 14.5 General safeguards

1. Stop writers and workers.
2. Verify exact target identifiers and resolved paths.
3. Capture PostgreSQL/blob recovery state when data has value.
4. Use dry-run modes where available.
5. Record command, actor, time, and result.
6. Run `doctor` after completion.

## 15. Recovery drill checklist

### 15.1 Full disaster recovery

- Restore PostgreSQL and matching blobs.
- Verify schema and hashes.
- Rebuild Qdrant.
- Recreate Valkey.
- Start worker and prove zero missing/orphaned points.
- Retrieve a known passage and resolve its provenance.

### 15.2 Index cutover recovery

- Build a second fingerprinted collection.
- Interrupt activation at a controlled point.
- Reconcile alias state.
- Finalize or roll back.
- Prove retrieval remains compatible.

### 15.3 Run recovery drill

- Interrupt a wrapper after invocation start.
- Verify the failure record.
- Retry with a new invocation ID.
- Complete indexing and finish the run.
- Confirm immutable revisions and events.

### 15.4 Endpoint failure drill

- Stop embedding or reranker endpoint.
- Verify explicit degraded/failure status.
- Restart with the same identity.
- Drain retryable work.
- Confirm no silent fallback.

The standalone checklist in `recovery-drill-checklist.md` provides the evidence fields and sign-off format.
