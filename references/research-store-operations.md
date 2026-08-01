<!-- @format -->

# Research Store Operations

This document is a compact operator reference. `operations-runbook.md` is the complete runbook.

## Configure persistence

Preserve explicit caller values. `scripts/research-env` fills only unset variables from the repository-root `.env` and container secret adapters.

```bash
export FIRECRAWL_RESEARCH_PERSIST=on
export FIRECRAWL_RESEARCH_PYTHON="$HOME/.codex/skills/firecrawl/.venv-research-store/bin/python"
source scripts/research-env
```

Modes:

- `on`: require a healthy PostgreSQL-backed store before acquisition and fail closed.
- `auto`: persist when `DATABASE_URL` resolves; otherwise allow scratch-only acquisition.
- `off`: create scratch diagnostics only; no PostgreSQL, blob, index-job, or Qdrant mutation.

Private scratch-only acquisition requires only:

```bash
FIRECRAWL_RESEARCH_PERSIST=off \
  rtk proxy scripts/fscrape 'https://example.com/private'
```

Never print or commit database URLs, API keys, or container secret contents.

## Initialize and inspect

The current clean PostgreSQL schema head is `0038_postgres_authority`. It intentionally does not upgrade databases created by the removed filesystem-compatibility migration chain. Reset the research datastores before first use of this baseline.

```bash
scripts/reset-firecrawl-research
scripts/research-db status
scripts/research-db ingest-ready
scripts/research-db doctor
```

`doctor` is read-only. It reports schema, blob integrity, worker heartbeat, pending/dead jobs, active index fingerprint, Qdrant coverage, Valkey reachability, and embedding/reranker health.

## Run the lease-safe worker

```bash
scripts/research-db worker \
  --batch-size 32 \
  --poll-seconds 5 \
  --lease-seconds 300 \
  --max-attempts 5
```

Production should use `firecrawl-research-indexer.service`. Valkey is only a wakeup optimization; workers always recover work by polling PostgreSQL.

## Use the PostgreSQL workflow

```bash
RUN_ID="$(scripts/frun start 'Research objective')"
scripts/fscrape 'https://example.com' --research-run-id "$RUN_ID"
scripts/fsearch 'bounded query' --research-run-id "$RUN_ID"
scripts/frun finish "$RUN_ID" --outcome satisfied
scripts/research-db run-status "$RUN_ID"
```

Wrappers validate the run before network work, record one PostgreSQL invocation, and advance the state machine through permitted transitions. `frun finish` verifies that run assets are indexed before completing the run. Retry uncertain commands with the same idempotency key; do not edit run or event rows manually.

## Build, activate, and roll back indexes

```bash
scripts/research-db index-list
scripts/research-db index-build --current-config --all
scripts/research-db worker --once --batch-size 64
scripts/research-db reconcile-qdrant
scripts/research-db index-activate '<index-id>'
scripts/research-db index-rollback '<prior-index-id>'
scripts/research-db index-prune --dry-run
```

Qdrant is a projection. Rebuild it from PostgreSQL chunks; never restore workflow truth from vectors.

## Ingest, rederive, and export diagnostics

```bash
scripts/research-db import-scratch '<scratch-dir>' --dry-run
scripts/research-db import-scratch '<scratch-dir>'
scripts/research-db rederive --snapshot '<snapshot-id>'
scripts/research-db export-invocation 'fc_<uuid>' --output invocation.json
scripts/research-db export-run 'fr_<uuid>' --output run.json
```

Exports are one-time JSON diagnostics. They are not a second database and are never read as workflow authority.

## Back up and recover

Capture PostgreSQL and the blob root at one recovery boundary. Record current Qdrant alias/index metadata for diagnosis, but rebuild vectors after restore. Valkey does not require backup.

Recovery order:

1. restore PostgreSQL;
2. restore the matching blob root;
3. run `verify-blobs` and `doctor`;
4. rebuild and activate the current Qdrant index;
5. restart the worker and verify zero missing/orphaned points.

## Validate before acceptance

Run the full deterministic and integration suites against a uniquely named disposable PostgreSQL database. Live acceptance must prove wrapper preflight, PostgreSQL/blob persistence, lease-safe indexing, Qdrant reconciliation, valid run transitions, endpoint restart recovery, and scratch-only non-persistence.
