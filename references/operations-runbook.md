<!-- @format -->

# Operations Runbook

Operational procedures for the PostgreSQL-authoritative Firecrawl Research Skill. `authoritative-workflows.md` is the canonical source for acquisition, completion, transaction, and projection-recovery command ordering.

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

## 1. Architecture overview

The deployed release implements Target A.

| Layer | Component | Contract | Recovery |
|---|---|---|---|
| Authoritative metadata and workflow | PostgreSQL | Runs, invocations, transitions, provenance, corpus identities, evidence, audits, jobs | Restore first |
| Immutable payload bytes | `BLOB_ROOT` | Content-addressed provider bytes referenced by PostgreSQL | Restore with the matching PostgreSQL boundary |
| Dense retrieval | Qdrant | Fingerprinted rebuildable projection behind `research_chunks_active` | Rebuild from PostgreSQL chunks |
| Coordination | Valkey | Optional wakeups and bounded transient state | Recreate; workers poll PostgreSQL |
| Process-local storage | Secure ephemeral files | Bounded implementation details only | Delete; never use as authority |

Target A deliberately does not store provider payload bytes in PostgreSQL. Acquisition installs payload bytes in `BLOB_ROOT` before PostgreSQL commits metadata that references their digest. A PostgreSQL rollback may leave an unreferenced orphan blob; committed metadata pointing to absent bytes is corruption.

A future PostgreSQL-payload design requires a separate schema, migration, capacity, backup, and rollback plan.

## 2. Service boundaries

### PostgreSQL

Authoritative for:

- `research_runs`, lifecycle revisions, transitions, events, and invocations;
- search responses, stable candidates, extraction attempts, sources, snapshots, documents, derivations, chunks, and run-asset links;
- research specifications, budgets, coverage, claims, evidence, semantic provenance, audits, and terminal decisions;
- index definitions, embedding manifests, jobs, leases, and worker heartbeats.

Never hand-edit append-only ledgers. Use service commands with current revisions and stable idempotency keys. Stable authoritative IDs are returned only after the corresponding transaction commits.

### `BLOB_ROOT`

Stores immutable payload bytes by digest. PostgreSQL snapshot rows carry the digest and byte length. A referenced missing or invalid blob is corruption; an unreferenced blob is a reportable orphan. Verify after backup, restore, and migration.

### Qdrant

Contains only dense vectors and payload necessary for retrieval projection. The active alias must target the exact configured embedding fingerprint and compatible schema. Qdrant cannot recreate workflow, provenance, or provider bytes.

### Valkey

Provides wakeups and bounded transient coordination. Loss may increase latency but cannot lose durable work.

### Firecrawl and model endpoints

A supported acquisition validates PostgreSQL, schema, privileges, blob durability, and run eligibility before constructing or invoking Firecrawl. Endpoint failures are explicit. No remote or non-persistent fallback is permitted.

## 3. Execution modes

- `agent_led`: the host agent supplies semantic decisions; deterministic services validate and persist them.
- `autonomous_local`: configured local models produce versioned semantic artifacts.
- `deterministic_debug`: fixtures replace semantic calls for reproducible tests.

All normal modes share the same authoritative persistence boundary. Normal research starts through `scripts/fresearch`; the deprecated `scripts/fsearch_smart` name is only an exact delegate to `fresearch run` and no longer owns separate dry-run, spec-skeleton, checkpoint, or recovery semantics.

## 4. Deployment

### 4.1 Prerequisites

- PostgreSQL 16 or compatible current server;
- writable durable `BLOB_ROOT`;
- Qdrant compatible with the pinned client;
- optional Valkey;
- Python dependencies from `requirements-research-store.txt`;
- Firecrawl Node CLI on `PATH`;
- configured embedding and reranking endpoints.

### 4.2 Environment

```bash
cd "<skill-root>"
export FIRECRAWL_RESEARCH_PYTHON="<skill-root>/.venv-research-store/bin/python"
source scripts/research-env
```

Explicit environment variables override repository defaults. Never print secrets.

### 4.3 Initialize

The current clean schema head is `0038_postgres_authority`.

```bash
scripts/research-db migrate
scripts/research-db status
scripts/research-db ingest-ready
scripts/research-db doctor
```

A database from an unsupported older lineage must be handled according to `migration-guide.md`; do not manually stamp it.

### 4.4 Worker service

Continuous service:

```bash
systemctl --user daemon-reload
systemctl --user enable --now firecrawl-research-indexer.service
systemctl --user --no-pager --full status firecrawl-research-indexer.service
```

Foreground equivalent:

```bash
scripts/research-db worker \
  --batch-size 32 \
  --poll-seconds 5 \
  --lease-seconds 300 \
  --max-attempts 5
```

`research-db worker --once` handles at most one bounded batch. For a deterministic complete drain, use:

```bash
python3 scripts/drain_index_jobs.py --batch-size 64
```

The helper returns success only after a batch reports `claimed=0`; it fails closed on invalid output, worker errors, failed jobs, lease loss, or an exceeded batch bound.

### 4.5 Acquisition smoke test

```bash
RUN_ID="$(
  scripts/frun start 'Deployment smoke test' \
    --run-mode curated \
    --mode autonomous_local
)"
scripts/frun prepare "$RUN_ID"

scripts/fscrape 'https://example.com' \
  --research-run-id "$RUN_ID" \
  --json

scripts/frun assets "$RUN_ID"
scripts/research-db run-status "$RUN_ID"
scripts/research-db doctor
scripts/frun cancel "$RUN_ID" --reason 'deployment acquisition smoke complete'
```

This is deliberately a specialist acquisition smoke test, not the normal research workflow. `frun prepare` is required before direct provider acquisition; the smoke test cancels rather than fabricating curated selection or completion. Any failed authoritative preflight must occur before Firecrawl or network invocation.

## 5. Configuration variables

### 5.1 Core

| Variable | Purpose |
|---|---|
| `FIRECRAWL_RESEARCH_PYTHON` | Python executable used by `research-db` |
| `DATABASE_URL` | Authoritative PostgreSQL connection |
| `BLOB_ROOT` | Immutable content-addressed payload root |
| `FIRECRAWL_RESEARCH_RUN_ID` | Default `fr_<uuid>` for acquisition wrappers |
| `FIRECRAWL_INVOCATION_ID` | Deliberate retry invocation default |
| `FIRECRAWL_API_URL` | Firecrawl endpoint |
| `FIRECRAWL_API_KEY` | Firecrawl credential |

### 5.2 Qdrant and models

| Variable | Purpose |
|---|---|
| `QDRANT_URL` | Qdrant endpoint |
| `QDRANT_API_KEY` | Qdrant credential |
| `QDRANT_ALIAS` | Stable active alias |
| `EMBEDDING_URL` | OpenAI-compatible embedding endpoint |
| `EMBEDDING_API_KEY` | Embedding credential |
| `EMBEDDING_MODEL` | Embedding model identity |
| `EMBEDDING_REVISION` | Immutable embedding revision |
| `EMBEDDING_DIMENSION` | Expected vector dimension |
| `RERANKER_URL` | Reranking endpoint |
| `RERANKER_API_KEY` | Reranking credential |
| `RERANKER_MODEL` | Reranker identity |
| `GENERATIVE_URL` | OpenAI-compatible generative endpoint |
| `GENERATIVE_API_KEY` | Generative endpoint credential |
| `GENERATIVE_MODEL` | Explicit generative model identity; no implicit fallback |
| `FIRECRAWL_LLM_LOCAL_BASE_URL` | Local generative endpoint |
| `FIRECRAWL_LLM_LOCAL_MODEL` | Local model identity |
| `FIRECRAWL_AUDIT_AUTO_SEMANTIC` | Automatic semantic audit control |

The current production embedding baseline is immutable except through an
explicit embedding-space migration:

| Parameter | Production value |
|---|---|
| Caller endpoint | `http://127.0.0.1:8004/v1/embeddings` |
| API model alias | `embed` |
| Underlying model | `Qwen/Qwen3-Embedding-0.6B` |
| Model revision | `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3` |
| Vector dimension | `1024` |
| Pooling | `last_token` |
| Dtype | `float16` |
| Direct TEI endpoint | `http://127.0.0.1:8005` |

Firecrawl callers use the port `8004` compatibility proxy, not the direct TEI
port. The API alias does not define the embedding space; fingerprint it with
the immutable underlying model revision and the `1024`-dimension contract.

### 5.3 Coordination and derivations

| Variable | Purpose |
|---|---|
| `VALKEY_URL` | Optional Valkey endpoint |
| `PARSER_VERSION` | Active parser identity |
| `NORMALIZATION_VERSION` | Active normalizer identity |
| `CHUNKER_VERSION` | Active chunker identity |
| `TOKENIZER_NAME` | Tokenizer for bounded passages and budgets |

## 6. Backup and restore

### 6.1 Backup

Stop writers or establish an equivalent consistent boundary.

```bash
systemctl --user stop firecrawl-research-indexer.service
pg_dump --format=custom --file=research.pg.dump "$DATABASE_URL"
find "$BLOB_ROOT" -type f -print0 | sort -z | xargs -0 sha256sum > blob-inventory.sha256
scripts/research-db status > status.json
scripts/research-db index-list > indexes.json
systemctl --user start firecrawl-research-indexer.service
```

PostgreSQL and `BLOB_ROOT` must be restored from the same logical boundary. Qdrant snapshots are optional acceleration. Valkey does not require backup.

### 6.2 Restore

1. Stop writers and workers.
2. Restore PostgreSQL.
3. Restore the matching `BLOB_ROOT`.
4. Verify schema and blobs.
5. Build a compatible Qdrant projection.
6. Drain all durable index jobs.
7. Reconcile and activate only a complete compatible index.
8. Restart the worker and run `doctor`.

```bash
scripts/research-db verify-blobs
scripts/research-db status
scripts/research-db index-build --current-config --all
python3 scripts/drain_index_jobs.py --batch-size 64
scripts/research-db reconcile-qdrant
scripts/research-db doctor
scripts/research-db index-activate '<index-id>'
```

Abort restore acceptance on failed/dead jobs, lease loss, missing blobs, missing or orphaned Qdrant points, or fingerprint mismatch.

### 6.3 Run-level blob verification

For specialist integrity diagnosis of one persisted research run, use the JSON-emitting verifier:

```bash
scripts/research-db run-verify 'fr_<uuid>'
```

Artifact states are explicit: `available` when the content-addressed blob exists and verifies; `missing` when the expected digest is absent from `BLOB_ROOT`; and `hash_mismatch` when the digest path exists but its bytes do not verify against the expected digest. File-only historical references without an authoritative digest remain unverified rather than becoming integrity proof.

Report status is likewise explicit: `passed` when every eligible digest/path pair verifies, `failed` when any eligible pair is missing or hash-mismatched, and `inconclusive` when no eligible digest-backed object can establish integrity. The CLI treats report production separately from report meaning: conclusive `passed` and `failed` reports exit `0`, while `inconclusive` exits `1` by default. `--allow-empty` changes only an inconclusive result to exit `0`.

Automation must inspect the JSON `status` and counters; process exit alone is not the integrity verdict. In particular, exit `0` does not convert a JSON `failed` report into a passing verification result.

## 7. Qdrant rebuild

```bash
scripts/research-db index-list
scripts/research-db index-build --current-config --all
python3 scripts/drain_index_jobs.py --batch-size 64
scripts/research-db reconcile-qdrant
scripts/research-db doctor
scripts/research-db index-activate '<index-id>'
```

Activation requires complete manifests, compatible schema, zero missing or orphaned expected points, and a successful probe. Preserve the prior collection for rollback.

```bash
scripts/research-db index-rollback '<prior-index-id>'
scripts/research-db index-prune --dry-run
```

Never prune the active or only verified rollback index.

## 8. Valkey loss handling

Job truth remains in PostgreSQL. Recreate or restart Valkey, then restart the worker.

```bash
scripts/research-db doctor
systemctl --user restart firecrawl-research-indexer.service
```

No corpus, workflow, or job repair is required solely because Valkey was lost. A deterministic validation may stop Valkey and use `drain_index_jobs.py` to prove PostgreSQL polling remains sufficient.

## 9. Endpoint restart

For embedding or reranking outages:

1. stop the worker for an extended outage;
2. restart the endpoint with the same model identity;
3. run `endpoint-health`, `resource-status`, and `doctor`;
4. restart the worker or run the fail-closed drain helper;
5. verify no failed, dead, missing, or incompatible work remains.

```bash
scripts/research-db endpoint-health
scripts/research-db resource-status
scripts/research-db doctor
python3 scripts/drain_index_jobs.py --batch-size 32
```

A model revision or vector-dimension change requires a new index definition and collection.

For PostgreSQL restart, stop writers, restart PostgreSQL, verify `status` and `ingest-ready`, then restart the worker. For Qdrant restart, reconcile and rebuild if needed.

## 10. Interrupted-run recovery

For a normal controller-owned run, start with the public typed surface:

```bash
scripts/fresearch status 'fr_<uuid>'
scripts/fresearch continue 'fr_<uuid>'
scripts/fresearch result 'fr_<uuid>'
```

Use deeper specialist reads only when the typed result indicates a blocker or when debugging infrastructure:

```bash
scripts/research-db run-status 'fr_<uuid>'
scripts/finspect invocations --run 'fr_<uuid>'
scripts/research-db doctor
```

Retry uncertain identical *specialist* input with its original idempotency key and invocation identity. A stale lifecycle revision requires a fresh status read before any new mutation. Do not delete failed calls, edit ledger rows, or reconstruct controller lifecycle steps manually.

If the operation committed corpus and job records, drain and verify the run before adding more acquisition or finishing:

```bash
python3 scripts/drain_index_jobs.py --batch-size 64
scripts/research-db run-status 'fr_<uuid>'
```

Low-level reopen remains an explicit specialist operation for intentional same-lifecycle work; it is not the normal continuation of a completed controller result. Material objective/scope change uses the durable `fresearch fork oa_<uuid> ...` child-run boundary. When specialist reopen is genuinely required:

```bash
scripts/frun reopen 'fr_<uuid>' --reason 'same-lifecycle specialist work required'
```

Cancel explicitly:

```bash
scripts/frun cancel 'fr_<uuid>' --reason 'operator request'
```

## 11. PostgreSQL workflow recovery

`fresearch` is the normal deterministic control plane. Specialist `fsearch`, `fscrape`, and `finspect scrape-candidates` record provider-facing invocation state through PostgreSQL services. The deprecated `fsearch_smart` name delegates to `fresearch run` and owns no separate workflow. Successful completion is determined from committed authoritative records; no file-mediated completion handoff exists.

Diagnose:

```bash
scripts/research-db run-status 'fr_<uuid>'
scripts/finspect attempts --run 'fr_<uuid>'
scripts/research-db verify-blobs
scripts/research-db doctor
```

Replay retained search results without provider execution:

```bash
scripts/finspect search-responses --run 'fr_<uuid>'
scripts/finspect replay-search '<search-response-uuid>'
```

Select stable candidates:

```bash
scripts/finspect scrape-candidates '<candidate-uuid>' \
  --idempotency-key '<stable-key>'
python3 scripts/drain_index_jobs.py --batch-size 64
```

Explicit exports are presentation outputs only:

```bash
scripts/research-db export-invocation 'fc_<uuid>' --output invocation.json
scripts/research-db export-run 'fr_<uuid>' --output run.json
```

## 12. Benchmarking

```bash
scripts/research-db benchmark run \
  --dataset tests/fixtures/benchmark/benchmark-v2.json \
  --output benchmark-results.json

scripts/research-db benchmark results --results-path benchmark-results.json

scripts/research-db benchmark report \
  --results-path benchmark-results.json \
  --output benchmark-report.md
```

Use versioned inputs, explicit execution mode, measured telemetry, and retained environment metadata. Do not label simulated metrics as production evidence.

## 13. Release evidence

CI must test the exact candidate SHA and record:

- Ruff lint and formatting;
- Python 3.12 results;
- documentation/parser and lifecycle contracts;
- blob-before-metadata ordering and failed-blob-write suppression;
- multi-batch worker drain behavior;
- disposable PostgreSQL, Qdrant, Valkey, worker, and recovery contracts;
- exact-head evidence artifact and digest.

Any code or configuration change invalidates earlier evidence.

## 14. Destructive commands

### `reset-firecrawl-research`

Destroys configured PostgreSQL, Qdrant, Valkey, and blob data. Use only after reviewing exact targets and backups.

### `index-prune --force`

Review `--dry-run`, identify one exact inactive target, and preserve rollback coverage.

### Blob deletion

`verify-blobs` is read-only. Never manually delete a referenced digest. Orphan cleanup requires a reviewed bounded procedure and matching PostgreSQL backup.

### General safeguards

1. Stop writers and workers.
2. Resolve exact targets.
3. Capture PostgreSQL and blob recovery state.
4. Use dry-run where available.
5. Record actor, command, time, and result.
6. Run `doctor`.

## 15. Recovery drill checklist

### Full disaster recovery

- Restore matching PostgreSQL and `BLOB_ROOT`.
- Verify schema and hashes.
- Build Qdrant and drain all durable jobs.
- Reconcile and activate Qdrant.
- Recreate Valkey.
- Start the worker.
- Retrieve a known bounded passage and resolve provenance.

### Index cutover recovery

- Build a second fingerprinted collection.
- Interrupt before activation.
- Resume and drain all jobs.
- Reconcile alias state.
- Complete or roll back.
- Prove compatible retrieval.

### Run recovery drill

- Interrupt after authoritative invocation start.
- Verify the persisted nonterminal state.
- For controller-owned work, resume from the typed public directive; for specialist operations, retry identical input only with the same idempotency key or close the failed attempt and create a new operation.
- Verify authoritative state and consume `fresearch result`; do not invent a low-level finish sequence for a normal controller run.

### Endpoint failure drill

- Stop an endpoint.
- Verify explicit failure or degradation.
- Restart with the same identity.
- Drain retryable work.
- Confirm no silent fallback.

See `recovery-drill-checklist.md` for evidence fields and `release-notes-rc9.md` for the compatibility and rollback boundary.

## Authoritative live validation

```bash
scripts/live_validate.py --profile focused --max-operations 40
scripts/live_validate.py --profile failure-path --max-operations 20
scripts/live_validate.py --profile full --max-operations 100
scripts/live_validate.py --profile focused --max-operations 40 --artifact-root ./validation-artifacts
```

Without `--artifact-root`, the versioned report is emitted to stdout. With it, only the final report and manifest are exported. They are never runtime inputs. The validator fails on provider activity after failed preflight, retained acquisition artifacts in monitored temporary storage, invalid blobs, incomplete run-scoped jobs, incompatible Qdrant state, or incomplete expected point coverage.
