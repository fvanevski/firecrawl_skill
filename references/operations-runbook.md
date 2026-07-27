<!-- @format -->

# Operations Runbook

Complete runbook for deploying, operating, debugging, benchmarking, and recovering the Firecrawl Research Skill platform. Every procedure is deterministic and idempotent. If a procedure fails partway, re-run it — the system is designed so that repeated execution converges to the correct state.

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
11. [Catalog import and export](#11-catalog-import-and-export)
12. [Benchmarking](#12-benchmarking)
13. [Destructive commands](#13-destructive-commands)
14. [Recovery drill checklist](#14-recovery-drill-checklist)

---

## 1. Architecture overview

The platform comprises five layers:

| Layer | Component | Role | Recovery rule |
|-------|-----------|------|---------------|
| **Authoritative state** | PostgreSQL | Workflow state, corpus, indices, events, budgets, audits | Restore first; never infer from Qdrant, Valkey, or filesystem |
| **Immutable payloads** | Content-addressed blob root | Raw and normalized source bytes | Restore alongside PostgreSQL; verify referenced hashes |
| **Retrieval projection** | Qdrant | Dense-retrieval vector index (versioned by embedding fingerprint) | Rebuildable from PostgreSQL chunks |
| **Transient coordination** | Valkey | Best-effort worker wakeups and bounded cache | Lose safely; worker recovers by polling PostgreSQL |
| **Compatibility artifacts** | Scratch directories, Catalog v5 | Debugging, audit, export | Regenerable from PostgreSQL + blobs |

**Governing rules:**

1. PostgreSQL is the sole authoritative workflow and metadata state.
2. Immutable payload bytes remain in content-addressed storage.
3. Qdrant remains rebuildable.
4. Valkey remains transient.
5. Scratch and Catalog outputs remain derived.
6. EvidencePacket v1 is the exclusive evidence input for report synthesis.
7. Host-agent and autonomous-local modes remain explicit and independently supported.
8. Model output is schema-validated and reference-validated before persistence.
9. Every report claim must resolve to exact packet passages.
10. Unsupported and qualified claims remain explicit.
11. A report cannot complete against a stale packet.
12. Semantic cache loss cannot lose authoritative workflow state.
13. Cached results must be revalidated against current references and policy.
14. Lease ownership remains per indexing job even when requests are batched.
15. Partial batch failure cannot falsely complete jobs.
16. Local endpoint outages and resource limits remain explicit.
17. Workflow state must remain resumable after endpoint or process restart.
18. No commercial or remote fallback occurs without explicit configuration.
19. Performance claims require measurement.
20. Legacy paths may be removed only after replacement behavior is validated.

---

## 2. Service boundaries

### PostgreSQL

- **Role:** Authoritative for all workflow state, corpus records, indexing jobs, retrieval events, budget snapshots, semantic call provenance, and audit records.
- **Recovery:** Custom-format dump (`pg_dump --format=custom`). Restore with `pg_restore`.
- **Backup frequency:** Before every migration, before every index activation, and on a scheduled basis.
- **Schema version:** Track with `research-db status`. The current Alembic head is `0008_legacy_adapter_comparisons`.

### Blob root

- **Role:** Immutable, content-addressed storage for raw and normalized source bytes.
- **Path:** Configured via `BLOB_ROOT` (default: `$HOME/.local/share/firecrawl/blobs`).
- **Recovery:** Restore alongside PostgreSQL. Verify with `research-db verify-blobs`.
- **Cleanup:** Report orphaned blobs with `research-db verify-blobs`, then require exact hash set and `--force` for any deletion.

### Qdrant

- **Role:** Dense-retrieval vector index. Versioned by embedding fingerprint.
- **Naming:** Physical collections are `research_chunks_<12-character-fingerprint>`. The active alias is `research_chunks_active`.
- **Recovery:** Rebuildable from PostgreSQL chunks. See [Section 7](#7-qdrant-rebuild).
- **Backup:** Not required for operation. A rebuild from PostgreSQL is sufficient.

### Valkey

- **Role:** Best-effort worker wakeups and bounded cache. Never authoritative.
- **Recovery:** Loss is safe. The worker recovers by polling PostgreSQL for pending jobs.
- **Configuration:** `VALKEY_URL` (default: derived from `research-env`).

### Local model endpoints

- **LLM:** `FIRECRAWL_LLM_LOCAL_BASE_URL` (default: `http://192.168.4.115:8002/v1`), model `chat`.
- **Embedding:** `EMBEDDING_URL`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSION`.
- **Reranker:** `RERANKER_URL`, `RERANKER_MODEL`.
- **Recovery:** Workers retry failed jobs after endpoint restart. No state corruption.

---

## 3. Execution modes

The system supports three execution modes, each with distinct semantic authority:

### `agent_led`

| Aspect | Detail |
|--------|--------|
| **Semantic authority** | Host agent (ChatGPT, Gemini, or other capable hosted model) |
| **Inner LLM calls** | Absent when a valid host-agent decision exists for the same decision point |
| **Default** | Set by the host-facing service |
| **Use case** | Outer agent interprets user intent, approves or supplies `ResearchSpec`, makes semantic decisions, reviews coverage, produces final report |

### `autonomous_local`

| Aspect | Detail |
|--------|--------|
| **Semantic authority** | Configured local LLM (via `model_gateway`) |
| **Inner LLM calls** | Each stage is independently retryable and resumable |
| **Default** | Set by the standalone `run-start` CLI |
| **Use case** | Local LLM generates `ResearchSpec`, proposes search plan, triages candidates, assesses coverage, maps evidence to claims, drafts report |

### `deterministic_debug`

| Aspect | Detail |
|--------|--------|
| **Semantic authority** | Explicit user-supplied plans or deterministic fixtures |
| **Inner LLM calls** | None — avoids generative semantic decisions |
| **Use case** | Testing, regression isolation, infrastructure diagnosis |
| **Note** | Marks semantic coverage fields as `unassessed` |

**Mode changes:** Use `research-db run-mode-change <external_id> <new_mode> --expected-revision <N> --idempotency-key <key> --requested-by <user> --approved-by <user> --reason <reason>`. Terminal runs must be reopened before changing mode. Changes record an append-only event and invalidate prior valid semantic artifacts.

---

## 4. Deployment

### 4.1 Prerequisites

- PostgreSQL (any version supported by SQLAlchemy 2.0)
- Qdrant (any version supported by `qdrant-client`)
- Valkey/Redis (optional — worker polls PostgreSQL as fallback)
- Node.js environment with `firecrawl-cli` installed globally
- Python 3.x with the skill's virtual environment
- Local model endpoints (LLM, embedding, reranker) — required for `autonomous_local`

### 4.2 Environment setup

```bash
# Source the research environment
source "<skill-root>/scripts/research-env"

# Or set variables explicitly
export DATABASE_URL='postgresql://research:...@localhost/research'
export BLOB_ROOT="$HOME/.local/share/firecrawl/blobs"
export FIRECRAWL_RESEARCH_PERSIST=auto
export QDRANT_URL="http://127.0.0.1:6333"
export VALKEY_URL="redis://research_app:password@127.0.0.1:56379/0"
export EMBEDDING_URL="http://127.0.0.1:8004"
export RERANKER_URL="http://127.0.0.1:8004"
```

### 4.3 Database initialization

```bash
# Run all pending migrations
rtk proxy "<skill-root>/scripts/research-db" migrate

# Verify schema is current
rtk proxy "<skill-root>/scripts/research-db" status

# Verify the store is writable
rtk proxy "<skill-root>/scripts/research-db" ingest-ready

# Run the full diagnostics report
rtk proxy "<skill-root>/scripts/research-db" doctor
```

### 4.4 Systemd worker service

Install the worker as a persistent user service:

```bash
# Create the service file
cat > ~/.config/systemd/user/firecrawl-research-indexer.service << 'EOF'
[Unit]
Description=Firecrawl Research Index Worker
After=network-online.target postgresql.service qdrant.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=<skill-root>/.venv-research-store/bin/python -m research_store.cli worker --batch-size 32 --poll-seconds 5 --lease-seconds 300 --max-attempts 5
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
EnvironmentFile=<skill-root>/scripts/research-env

[Install]
WantedBy=default.target
EOF

# Enable and start
systemctl --user daemon-reload
systemctl --user enable --now firecrawl-research-indexer.service
systemctl --user status firecrawl-research-indexer.service
```

**Security hardening:**

- Keep the home and system trees read-only except for the resolved blob root.
- Use `NoNewPrivileges` and `PrivateTmp`.
- Order after network availability and database services.

### 4.5 Persistence modes

| Mode | Value | Behavior |
|------|-------|----------|
| **Auto** | `auto` | Persist when `DATABASE_URL` resolves; otherwise retain filesystem workflow |
| **On** | `on` | Validate research-store environment before acquisition; fail if unavailable |
| **Off** | `off` | Write no database records or raw corpus blobs |

Private runs disable both durable paths:

```bash
FIRECRAWL_CATALOG_DISABLED=1 FIRECRAWL_RESEARCH_PERSIST=off \
  rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/private"
```

---

## 5. Configuration variables

All configuration variables with their defaults, effects, and constraints.

### 5.1 Core persistence

| Variable | Default | Effect | Constraints |
|----------|---------|--------|-------------|
| `FIRECRAWL_RESEARCH_PERSIST` | `auto` | Controls database/blob persistence mode | `auto`, `on`, `off` |
| `DATABASE_URL` | Derived from `research-env` | PostgreSQL connection string | Required for `on` mode; takes precedence over `research-env` |
| `BLOB_ROOT` | `$HOME/.local/share/firecrawl/blobs` | Content-addressed blob storage root | Must be writable; read-only for systemd service except this path |

### 5.2 Vector retrieval

| Variable | Default | Effect | Constraints |
|----------|---------|--------|-------------|
| `QDRANT_URL` | `http://127.0.0.1:6333` | Qdrant HTTP endpoint | Optional — system degrades to lexical search if unavailable |
| `QDRANT_API_KEY` | Derived from `research-env` | Qdrant API key | Required if Qdrant requires authentication |
| `QDRANT_COLLECTION` | `research_chunks_v1` | Default Qdrant collection name | Informational; physical collections are fingerprint-named |

### 5.3 Transient coordination

| Variable | Default | Effect | Constraints |
|----------|---------|--------|-------------|
| `VALKEY_URL` | Derived from `research-env` | Valkey connection URL | Optional — worker falls back to polling PostgreSQL |

### 5.4 Model endpoints

| Variable | Default | Effect | Constraints |
|----------|---------|--------|-------------|
| `FIRECRAWL_LLM_LOCAL_BASE_URL` | `http://192.168.4.115:8002/v1` | Local LLM endpoint | Required for `autonomous_local` mode |
| `FIRECRAWL_LLM_LOCAL_MODEL` | `chat` | Local LLM model name | |
| `FIRECRAWL_AUDIT_LOCAL_*` | Legacy | Legacy audit endpoint variables | Accepted for backward compatibility |
| `EMBEDDING_URL` | Derived from `research-env` | Embedding endpoint | Required for indexing |
| `EMBEDDING_API_KEY` | | Embedding API key | |
| `EMBEDDING_MODEL` | | Embedding model name | |
| `EMBEDDING_REVISION` | | Embedding model revision | |
| `EMBEDDING_DIMENSION` | | Embedding dimension | Must match Qdrant collection schema |
| `RERANKER_URL` | Derived from `research-env` | Reranker endpoint | Required for reranking in retrieval |
| `RERANKER_API_KEY` | | Reranker API key | |
| `RERANKER_MODEL` | | Reranker model name | |
| `RERANKER_CANDIDATE_LIMIT` | | Maximum reranker candidates | |

### 5.5 Derivation versions

| Variable | Default | Effect | Constraints |
|----------|---------|--------|-------------|
| `PARSER_VERSION` | Derived from code | Document parser version | |
| `NORMALIZATION_VERSION` | Derived from code | Document normalization version | |
| `CHUNKER_VERSION` | Derived from code | Text chunker version | |

### 5.6 Catalog and scratch

| Variable | Default | Effect | Constraints |
|----------|---------|--------|-------------|
| `FIRECRAWL_CATALOG_DIR` | `$XDG_DATA_HOME/firecrawl` | Persistent catalog root | |
| `FIRECRAWL_CATALOG_DISABLED` | unset | Disable catalog for private runs | Set to `1` |
| `FIRECRAWL_AUDIT_AUTO_SEMANTIC` | `1` | Auto-run LLM audits on completed runs | Set to `0` to disable |
| `FIRECRAWL_LEGACY_ADAPTER_MODE` | `compatibility` | Legacy adapter behavior | `compatibility`, `shadow`, `authoritative` |
| `FIRECRAWL_RESEARCH_AUTO_ENV` | `1` | Auto-source `research-env` | Set to `0` to disable |
| `FIRECRAWL_RESEARCH_PYTHON` | `python3` | Python executable for research scripts | Must be valid if set |
| `FIRECRAWL_SEARCH_RETRIES` | `2` | Transient acquisition retry count | |
| `FIRECRAWL_RESEARCH_RUN_ID` | | Explicit run linkage | |

### 5.7 Budget and legacy

| Variable | Default | Effect | Constraints |
|----------|---------|--------|-------------|
| (none) | — | Legacy `--complexity` flag is retired | Accepted as diagnostic metadata only |

**Never print or commit credentials.** All API keys and connection strings are sensitive.

---

## 6. Backup and restore

### 6.1 Backup procedure

Before any production operation (migration, index activation, embedding change):

```bash
# 1. PostgreSQL custom-format dump
pg_dump --format=custom --file="/tmp/firecrawl-backup-$(date +%Y%m%d-%H%M%S).dump" "$DATABASE_URL"

# 2. Blob inventory with hashes
find "$BLOB_ROOT" -type f -exec sha256sum {} + > "/tmp/blob-inventory-$(date +%Y%m%d-%H%M%S).txt" 2>/dev/null || true

# 3. Qdrant collection and alias state
rtk proxy "<skill-root>/scripts/research-db" index-list

# 4. Doctor and status reports
rtk proxy "<skill-root>/scripts/research-db" status
rtk proxy "<skill-root>/scripts/research-db" doctor
```

**Stop ingestion** before capturing a consistent boundary:

```bash
# Stop the worker temporarily
systemctl --user stop firecrawl-research-indexer.service
```

### 6.2 Restore procedure

Restore in this exact order:

```bash
# 1. Restore PostgreSQL
pg_restore --dbname="$DATABASE_URL" --clean --if-exists "/tmp/firecrawl-backup-YYYYMMDD-HHMMSS.dump"

# 2. Restore blob root (if backed up)
# rsync or cp the blob directory to BLOB_ROOT

# 3. Verify blob integrity
rtk proxy "<skill-root>/scripts/research-db" verify-blobs

# 4. Rebuild the configured index
rtk proxy "<skill-root>/scripts/research-db" index-build --current-config --all

# 5. Drain the worker (process all queued jobs)
rtk proxy "<skill-root>/scripts/research-db" worker --once --batch-size 64

# 6. Reconcile Qdrant coverage
rtk proxy "<skill-root>/scripts/research-db" reconcile-qdrant

# 7. Activate the rebuilt index
rtk proxy "<skill-root>/scripts/research-db" index-activate "<index-id>"

# 8. Verify everything
rtk proxy "<skill-root>/scripts/research-db" doctor
```

### 6.3 Rollback after failed rollout

```bash
# 1. Stop the worker
systemctl --user stop firecrawl-research-indexer.service

# 2. Set persistence to off (prevent new writes)
export FIRECRAWL_RESEARCH_PERSIST=off

# 3. Restore PostgreSQL and blobs from the captured recovery point
pg_restore --dbname="$DATABASE_URL" --clean --if-exists "/tmp/pre-failure-backup.dump"

# 4. Switch the active alias back to the retained prior collection
rtk proxy "<skill-root>/scripts/research-db" index-rollback "<prior-index-id>"

# 5. Restart the worker
systemctl --user start firecrawl-research-indexer.service
```

---

## 7. Qdrant rebuild

Qdrant is rebuildable from PostgreSQL. Physical collections are named by embedding fingerprint; the alias `research_chunks_active` is switched only after verification.

### 7.1 Build a new index

```bash
# List existing indexes
rtk proxy "<skill-root>/scripts/research-db" index-list

# Build a new physical collection for the current embedding config
rtk proxy "<skill-root>/scripts/research-db" index-build --current-config --all

# Or build for a specific document
rtk proxy "<skill-root>/scripts/research-db" index-build --current-config --document "<document-id>"

# Process the jobs with the worker
rtk proxy "<skill-root>/scripts/research-db" worker --once --batch-size 64
```

### 7.2 Verify before activation

Activation requires:

1. All active-derivation manifests are complete.
2. Zero missing or orphaned active-derivation points in Qdrant.
3. Compatible collection schema (embedding dimension, distance metric).
4. A successful probe retrieval query.

```bash
# Reconcile Qdrant — detect missing points
rtk proxy "<skill-root>/scripts/research-db" reconcile-qdrant
```

### 7.3 Activate

```bash
rtk proxy "<skill-root>/scripts/research-db" index-activate "<index-id>"
```

The alias switch is atomic. The previous collection is retained for rollback.

### 7.4 Rollback

```bash
rtk proxy "<skill-root>/scripts/research-db" index-rollback "<prior-index-id>"
```

### 7.5 Prune old collections

```bash
# Dry run — review what would be deleted
rtk proxy "<skill-root>/scripts/research-db" index-prune --dry-run

# Keep the last 2 collections
rtk proxy "<skill-root>/scripts/research-db" index-prune --dry-run --keep-last 2

# Force delete a specific collection (NEVER the active index)
rtk proxy "<skill-root>/scripts/research-db" index-prune --force --index-id "<exact-index-id>"
```

**Never prune the active index.** Always verify the active alias before pruning.

### 7.6 Interrupted cutover recovery

If activation was interrupted after the "prepared" state:

1. Read the activation journal via `index-list`.
2. Do not start another cutover until the interrupted state is reconciled.
3. If the alias is already pointing to the new collection, the cutover succeeded — verify with `doctor`.
4. If the alias is stale, complete the activation or rollback.

---

## 8. Valkey loss handling

Valkey is transient. Loss of Valkey state is **safe** and requires **no action**.

### 8.1 Why it is safe

- The worker polls PostgreSQL for pending jobs as the primary mechanism.
- Valkey notifications only shorten polling latency.
- No workflow state, corpus records, or indexing jobs are stored in Valkey.

### 8.2 When Valkey is lost

- The worker continues to process jobs on its poll cycle (default: 5 seconds).
- No jobs are lost. No state is corrupted.
- `doctor` reports Valkey reachability; a failure is informational only.

### 8.3 When to care

- If polling latency is unacceptable (e.g., real-time ingestion pipelines).
- Reconnect Valkey and restart the worker to resume notification-based wakeups.

---

## 9. Endpoint restart

### 9.1 Local model endpoint (LLM, embedding, reranker)

When a local model endpoint restarts:

1. **During active work:** The worker or orchestrator that was calling the endpoint will receive a connection error. The call is recorded as a failed semantic call or failed job in PostgreSQL.
2. **After restart:** The worker retries expired-running leases. Semantic calls left in `running` state after process loss may be finalized as failed by forward repair.
3. **No state corruption:** Failed calls are queryable. Failed indexing jobs are retryable.

### 9.2 Qdrant restart

1. Qdrant upserts are idempotent when a worker crashes after projection but before completion.
2. After restart, the worker continues processing remaining jobs.
3. If a physical collection was deleted or damaged, `reconcile-qdrant` detects and requeues missing points even when prior jobs say complete.

### 9.3 PostgreSQL restart

1. PostgreSQL transactions are atomic. In-flight transactions roll back.
2. Leases held by workers become expired. The worker's next poll reclaims them.
3. No state is lost. Jobs remain in the queue.

### 9.4 Recovery after all endpoint failures

```bash
# 1. Check doctor for health status
rtk proxy "<skill-root>/scripts/research-db" doctor

# 2. Check for stale or dead leases
# (included in doctor output)

# 3. Restart the worker to drain remaining jobs
systemctl --user restart firecrawl-research-indexer.service

# 4. Verify worker heartbeat is current
rtk proxy "<skill-root>/scripts/research-db" doctor
```

---

## 10. Interrupted-run recovery

### 10.1 State machine basics

Research runs transition through explicit states:

```
created -> planning -> corpus_review -> acquiring -> extracting -> indexing ->
coverage_review -> retrieving -> synthesizing -> validating -> completed/partial/failed
```

Terminal states (`completed`, `partial`, `failed`, `cancelled`) reject ordinary transitions.

### 10.2 Diagnosing an interrupted run

```bash
# Check current state and revision
rtk proxy "<skill-root>/scripts/research-db" run-status "<run-id>"

# Review transition and event ledgers
# (included in run-status output)

# Check for running semantic calls that may be orphaned
# (included in doctor output)
```

### 10.3 Forward repair

```bash
# Retry an uncertain command with the same idempotency key
rtk proxy "<skill-root>/scripts/research-db" run-transition "<run-id>" "<next-state>" \
  --expected-revision <N> \
  --idempotency-key "<same-key-as-before>" \
  --actor "operator"

# If the revision has changed, read status and decide whether a genuinely new command is valid
rtk proxy "<skill-root>/scripts/research-db" run-status "<run-id>"
```

### 10.4 Reopening a terminal run

```bash
rtk proxy "<skill-root>/scripts/research-db" run-reopen "<run-id>" \
  --reason "add missing official corroboration" \
  --expected-revision <N> \
  --idempotency-key "reopen-$(uuidgen)"
```

Reopen moves a terminal run to `created`, increments the revision, records `reopened_from_revision`, and marks prior valid semantic artifacts invalid without deleting their provenance.

### 10.5 Canceling a run

```bash
rtk proxy "<skill-root>/scripts/research-db" run-cancel "<run-id>" \
  --reason "operator request" \
  --expected-revision <N> \
  --idempotency-key "cancel-$(uuidgen)"
```

### 10.6 Semantic call recovery

A semantic call left in `running` state after process loss:

- May be finalized as failed by forward repair.
- May be retried with the same call idempotency key.
- **Never delete** its attempt or artifact provenance.

---

## 11. Catalog import and export

### 11.1 Import scratch files

```bash
# Dry run — review what would be imported
rtk proxy "<skill-root>/scripts/research-db" import-scratch "$SCRATCH_ROOT" --dry-run --report /tmp/import-dry.json

# Apply the import (idempotent — safe to retry)
rtk proxy "<skill-root>/scripts/research-db" import-scratch "$SCRATCH_ROOT" --report /tmp/import.json
```

### 11.2 Export invocation to compatibility format

```bash
rtk proxy "<skill-root>/scripts/research-db" export-invocation "fc_<uuid>" --output _corpus.json
```

Use when the database commit succeeded but the compatibility export was interrupted.

### 11.3 Catalog v5 export

```bash
# Export a specific run to catalog format
rtk proxy "<skill-root>/scripts/research-db" catalog-export run "<external-id>" --target-dir /tmp/catalog-output

# Export by invocation ID
rtk proxy "<skill-root>/scripts/research-db" catalog-export invocation "<invocation-id>" "<run-id>" --target-dir /tmp/catalog-output

# Export events
rtk proxy "<skill-root>/scripts/research-db" catalog-export events "<external-id>" --target-dir /tmp/catalog-output

# Regenerate all catalog records for an external ID
rtk proxy "<skill-root>/scripts/research-db" catalog-export regenerate "<external-id>" --target-dir /tmp/catalog-output
```

### 11.4 Catalog v5 maintenance

```bash
# Purge stale data (dry run by default)
rtk proxy "<skill-root>/scripts/frun" purge

# Purge with force
rtk proxy "<skill-root>/scripts/frun" purge --force

# Purge by date
rtk proxy "<skill-root>/scripts/frun" purge --before 2026-07-01T00:00:00Z

# Purge by count
rtk proxy "<skill-root>/scripts/frun" purge --keep-last 10

# Purge orphans
rtk proxy "<skill-root>/scripts/frun" purge --orphans

# Migrate catalog schema (dry run)
rtk proxy "<skill-root>/scripts/frun" migrate --from 4 --to 5

# Apply migration (discards old catalog — no backup)
rtk proxy "<skill-root>/scripts/frun" migrate --from 4 --to 5 --apply
```

**Schema transition warning:** `migrate --apply` discards the entire old catalog without conversion or backup. Always verify the database is in a consistent state before migrating.

---

## 12. Benchmarking

### 12.1 Run a benchmark campaign

```bash
# Run benchmark against a fixed dataset
rtk proxy "<skill-root>/scripts/research-db" benchmark run \
  --dataset /path/to/benchmark-dataset.json \
  --modes agent_led autonomous_local \
  --output /tmp/benchmark-results.json

# Exit code: 0 = go or go_with_conditions (both are successes)
# Exit code: 2 = no_go (failure)
# Parse the JSON 'outcome' field to distinguish between go and go_with_conditions
```

### 12.2 View results

```bash
rtk proxy "<skill-root>/scripts/research-db" benchmark results \
  --results-path /tmp/benchmark-results.json
```

### 12.3 Generate a report

```bash
rtk proxy "<skill-root>/scripts/research-db" benchmark report \
  --results-path /tmp/benchmark-results.json \
  --output /tmp/benchmark-report.txt
```

### 12.4 Benchmark configuration

Benchmarks measure:

- **Throughput:** Documents processed per minute.
- **Latency:** Time per stage (acquisition, extraction, indexing, retrieval, synthesis).
- **Quality:** Citation accuracy, claim support, evidence coverage.
- **Resource usage:** Endpoint concurrency, model residency assumptions, backpressure.

Benchmarks are reproducible when run against fixed fixture datasets. The `generate_benchmark_fixtures.py` script creates deterministic fixture data.

### 12.5 Legacy versus current benchmark

```bash
# Benchmark the current system
rtk proxy "<skill-root>/scripts/research-db" benchmark run \
  --dataset /path/to/fixture-dataset.json \
  --modes agent_led autonomous_local \
  --no-dry-run \
  --output /tmp/benchmark-current.json

# Compare with legacy behavior by running the legacy adapter in shadow mode
FIRECRAWL_LEGACY_ADAPTER_MODE=shadow \
  rtk proxy "<skill-root>/scripts/research-db" benchmark run \
  --dataset /path/to/fixture-dataset.json \
  --modes legacy \
  --output /tmp/benchmark-legacy.json
```

---

## 13. Destructive commands

Every destructive command is documented with its scope, safeguards, and recovery procedure.

### 13.1 `index-prune --force`

| Aspect | Detail |
|--------|--------|
| **What it does** | Permanently deletes a Qdrant physical collection |
| **Scope** | One specific collection identified by `--index-id` |
| **Safeguard** | Never prunes the active index; requires `--force` flag |
| **Recovery** | Rebuild the collection with `index-build --current-config --all` |
| **Before use** | Verify the index ID is not the active index via `index-list` |

### 13.2 `migrate --from N --to M --apply`

| Aspect | Detail |
|--------|--------|
| **What it does** | Discards the entire old Catalog v5 schema and initializes an empty catalog at the new schema |
| **Scope** | One catalog root |
| **Safeguard** | Dry run by default; `--apply` required to execute |
| **Recovery** | No automatic recovery. Restore from a prior catalog backup if available. Database state is unaffected. |
| **Before use** | Ensure the database is in a consistent state; stop all ingestion. |

### 13.3 `purge --force` (no filter)

| Aspect | Detail |
|--------|--------|
| **What it does** | Removes the entire resolved catalog root |
| **Scope** | One catalog root |
| **Safeguard** | Requires `--force`; no filter removes everything |
| **Recovery** | Regenerate from PostgreSQL and blob storage via `catalog-export` commands |
| **Before use** | Verify the catalog root path; consider `--keep-last` or `--before` instead |

### 13.4 `verify-blobs` with `--force` deletion

| Aspect | Detail |
|--------|--------|
| **What it does** | Deletes blobs not referenced by any snapshot |
| **Scope** | All unreferenced blobs in `BLOB_ROOT` |
| **Safeguard** | First reports orphans; requires exact hash set and `--force` for deletion |
| **Recovery** | Not recoverable. Re-import from source if needed. |
| **Before use** | Always run without `--force` first to review the orphan list |

### 13.5 Manual job/lease editing

| Aspect | Detail |
|--------|--------|
| **What it does** | Direct SQL manipulation of jobs, leases, or state tables |
| **Scope** | Arbitrary PostgreSQL tables |
| **Safeguard** | **Not recommended.** Use CLI commands instead. |
| **Recovery** | Restore from PostgreSQL backup. |
| **Before use** | **Do not do this.** Use `run-reopen`, `run-transition`, or `index-build` commands. |

### 13.6 General safeguards for all destructive operations

1. **Always capture a backup first** (see [Section 6.1](#61-backup-procedure)).
2. **Stop ingestion** (`systemctl --user stop firecrawl-research-indexer.service`).
3. **Run dry-run mode** where available.
4. **Verify the target** (index ID, catalog path, blob hashes).
5. **Document the operation** in the event ledger or a maintenance log.
6. **Re-verify** with `doctor` after the operation.

---

## 14. Recovery drill checklist

Use this checklist periodically to verify that recovery procedures work end-to-end.

### 14.1 Full disaster recovery

- [ ] **Step 1:** Capture a consistent backup (PostgreSQL dump + blob inventory + Qdrant state).
- [ ] **Step 2:** Stop all services (worker, any active ingestion).
- [ ] **Step 3:** Simulate data loss (drop the database or move blob root).
- [ ] **Step 4:** Restore PostgreSQL from backup.
- [ ] **Step 5:** Restore blob root.
- [ ] **Step 6:** Verify blob integrity (`research-db verify-blobs`).
- [ ] **Step 7:** Rebuild the index (`research-db index-build --current-config --all`).
- [ ] **Step 8:** Drain the worker (`research-db worker --once --batch-size 64`).
- [ ] **Step 9:** Reconcile Qdrant (`research-db reconcile-qdrant`).
- [ ] **Step 10:** Activate the index (`research-db index-activate "<id>"`).
- [ ] **Step 11:** Verify with `doctor` — all components healthy.
- [ ] **Step 12:** Restart the worker.
- [ ] **Step 13:** Run a test acquisition and verify end-to-end provenance.

### 14.2 Index cutover recovery

- [ ] **Step 1:** Build a new index.
- [ ] **Step 2:** Simulate cutover interruption (stop worker mid-activation).
- [ ] **Step 3:** Check `index-list` for the interrupted state.
- [ ] **Step 4:** Complete the activation or rollback.
- [ ] **Step 5:** Verify the active alias via `doctor`.

### 14.3 Run recovery drill

- [ ] **Step 1:** Start a test run (`run-start`).
- [ ] **Step 2:** Transition it to a non-terminal state.
- [ ] **Step 3:** Simulate interruption (kill the process).
- [ ] **Step 4:** Check `run-status` for the interrupted state.
- [ ] **Step 5:** Reopen the run.
- [ ] **Step 6:** Resume work and complete the run.

### 14.4 Endpoint failure drill

- [ ] **Step 1:** Stop the local LLM endpoint.
- [ ] **Step 2:** Start an `autonomous_local` run that requires the LLM.
- [ ] **Step 3:** Verify the failure is recorded in PostgreSQL (not silently swallowed).
- [ ] **Step 4:** Restart the endpoint.
- [ ] **Step 5:** Verify the worker retries and completes.

---

*This runbook is a living document. Update it when new failure modes are discovered, new procedures are validated, or new configuration variables are added. Cross-reference `research-store-architecture.md` for authority boundaries and `workflow-state-schema.md` for the state machine.*
