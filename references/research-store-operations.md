# Research Store Operations

## Configure persistence

Preserve explicit caller values. Let `scripts/research-env` fill only unset variables from the configured host adapter; never overwrite a supplied database, service endpoint, key, blob root, or Python executable.

```bash
export FIRECRAWL_RESEARCH_PERSIST=auto
export DATABASE_URL='postgresql://research:...@localhost/research'
export BLOB_ROOT="$HOME/.local/share/firecrawl/blobs"
```

Set `FIRECRAWL_RESEARCH_PERSIST` as follows:

- `auto`: persist when `DATABASE_URL` resolves; otherwise retain the filesystem workflow.
- `on`: validate the research-store environment before acquisition and fail if it is unavailable.
- `off`: write no database records or raw corpus blobs.

For private acquisition, disable both durable paths:

```bash
FIRECRAWL_CATALOG_DISABLED=1 FIRECRAWL_RESEARCH_PERSIST=off \
  rtk proxy scripts/fscrape 'https://example.com/private'
```

Resolve `FIRECRAWL_RESEARCH_PYTHON` once. Fail clearly if an explicit path is invalid; otherwise prefer the bundled executable virtual environment and fall back to `python3`. Optional settings include `QDRANT_URL`, `QDRANT_API_KEY`, `VALKEY_URL`, `EMBEDDING_URL`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`, `EMBEDDING_REVISION`, `EMBEDDING_DIMENSION`, `RERANKER_URL`, `RERANKER_API_KEY`, `RERANKER_MODEL`, `RERANKER_CANDIDATE_LIMIT`, `PARSER_VERSION`, `NORMALIZATION_VERSION`, and `CHUNKER_VERSION`. Never print or commit credentials.

## Migrate and inspect

Use Alembic as the only schema authority. The current head is `0005_run_lifecycle`; v3 adds the composite manifest/definition job constraint, v4 repairs the stale v1 embedding uniqueness constraint, and v5 enforces terminal research-run lifecycle invariants. Apply revisions explicitly and verify the reported current/head revisions. Exercise v1 through v5 on a disposable non-empty, multi-index database before production.

```bash
rtk proxy scripts/research-db migrate
rtk proxy scripts/research-db status
rtk proxy scripts/research-db ingest-ready
rtk proxy scripts/research-db doctor
```

Treat `doctor` as read-only. It reports database/schema access, blob readability and reference integrity, worker heartbeat, oldest pending job, stale leases, dead jobs, failed batches, active index fingerprint, Qdrant alias/coverage, Valkey reachability, and embedding/reranker health. It must not create a blob probe, collection, alias, directory, migration, or cache record.

Before a production migration, capture a PostgreSQL custom-format dump, blob inventory with hashes, Qdrant collection/alias state, and `status`, `doctor`, and reconciliation output. Stop ingestion or record a consistent database/blob boundary. Never transform or delete the prior Qdrant collection during the migration.

## Run the lease-safe worker

Run one or more durable workers. PostgreSQL is the queue of record; Valkey notifications only shorten polling latency.

```bash
rtk proxy scripts/research-db worker \
  --batch-size 32 --poll-seconds 5 --lease-seconds 300 --max-attempts 5
rtk proxy scripts/research-db worker --once --batch-size 32
```

Install `firecrawl-research-indexer.service` as a lingering user-systemd unit. Execute the skill virtualenv and `research-db worker`, restart after five seconds, order after network availability, log to the journal, and enable `NoNewPrivileges` plus `PrivateTmp`. Keep the home and system trees read-only except for the resolved blob root. Do not hard-require embedding, reranker, Qdrant, or Valkey services; durable retryable jobs tolerate temporary outages.

```bash
systemctl --user daemon-reload
systemctl --user enable --now firecrawl-research-indexer.service
systemctl --user status firecrawl-research-indexer.service
journalctl --user -u firecrawl-research-indexer.service
```

An expired running lease is reclaimable. A completion with a stale lease token is rejected. Jobs exceeding five attempts become visible dead jobs. Do not clear or edit jobs manually; diagnose their exact manifest and endpoint error first.

## Build, activate, and roll back indexes

Create one immutable index definition for each embedding fingerprint. Build its physical `research_chunks_<fingerprint>` collection without disturbing `research_chunks_active`.

```bash
rtk proxy scripts/research-db index-list
rtk proxy scripts/research-db index-build --current-config --all
rtk proxy scripts/research-db index-build --current-config --document '<document-id>'
rtk proxy scripts/research-db worker --once --batch-size 64
rtk proxy scripts/research-db reconcile-qdrant
rtk proxy scripts/research-db index-activate '<index-id>'
```

Require complete active-derivation manifests, zero missing or orphaned active-derivation points, a compatible collection schema, and a successful probe retrieval before activation. A rebuild detects and requeues points missing from a deleted or damaged physical collection even when prior jobs say complete. Switch the alias atomically and retain the previous collection. Recover interrupted prepared/switched activation journals before another cutover. Run query and worker processes with configuration matching the active fingerprint; an alias mismatch disables dense retrieval and makes `doctor` unhealthy.

```bash
rtk proxy scripts/research-db index-rollback '<prior-index-id>'
rtk proxy scripts/research-db index-prune --dry-run
rtk proxy scripts/research-db index-prune --dry-run --keep-last 2
rtk proxy scripts/research-db index-prune --force --index-id '<exact-index-id>'
```

Never prune the active index. Keep `index-once` only as a compatibility alias for `worker --once`; treat `reindex` as a deprecated alias for `index-build`.

## Ingest, rederive, and reconstruct exports

Enabled wrapper persistence is fail-closed. Commit all successful assets plus explicit per-asset failures in one invocation batch, using savepoints to isolate individual failures. Preserve diagnostic scratch files and write the partial `_corpus.json`, but return nonzero when any successful Firecrawl result cannot be retained.

```bash
rtk proxy scripts/research-db import-scratch "$SCRATCH_ROOT" --dry-run --report /tmp/import-dry.json
rtk proxy scripts/research-db import-scratch "$SCRATCH_ROOT" --report /tmp/import.json
rtk proxy scripts/research-db rederive --all
rtk proxy scripts/research-db rederive --snapshot '<snapshot-id>'
rtk proxy scripts/research-db export-invocation 'fc_<uuid>' --output _corpus.json
```

Use `rederive` after parser, normalization, or chunker changes; do not manufacture a new snapshot. Use `export-invocation` when the database commit succeeded but the compatibility export was interrupted. Report blobs without snapshot references before cleanup; require an exact hash set and `--force` for any deletion.

## Link runs and retrieve evidence

Mirror each Catalog v5 `fr_<uuid>` lifecycle in the database. Link wrapper batches and their assets to it, then record retrieval and selection events against the same run.

```bash
RUN_ID="$(rtk proxy scripts/frun start 'Research objective' --profile auto)"
rtk proxy scripts/fsearch_smart 'Research objective' --research-run-id "$RUN_ID"
rtk proxy scripts/research-db search-assets 'evidence query' --research-run-id "$RUN_ID" --limit 20
rtk proxy scripts/research-db fetch-passages '<candidate-id>' --research-run-id "$RUN_ID" --max-tokens 2000
rtk proxy scripts/frun finish "$RUN_ID" --outcome satisfied --source-manifest sources.json --answer-file final.md
```

Start with compact manifests. Expand only selected candidates into bounded passages. Treat exports as non-authoritative debugging/audit copies.

## Back up and recover

Back up PostgreSQL and the blob root at one consistent recovery point. Qdrant and Valkey do not require authoritative backups. Restore PostgreSQL and blobs first, verify all referenced hashes, rebuild the configured index, drain the worker, reconcile coverage, then activate it.

```bash
rtk proxy scripts/research-db verify-blobs
rtk proxy scripts/research-db index-build --current-config --all
rtk proxy scripts/research-db worker --once --batch-size 64
rtk proxy scripts/research-db reconcile-qdrant
rtk proxy scripts/research-db index-activate '<index-id>'
rtk proxy scripts/research-db doctor
```

If a rollout fails, stop the worker, set persistence to `off`, restore PostgreSQL and blobs from the captured recovery point, and switch the active alias back to the retained prior collection.

## Validate before acceptance

Run the deterministic classifier, workflow, and research-store suites. Point `test_research_store_integration.py` only at a uniquely named disposable PostgreSQL database containing a standalone `test` segment, and set `RESEARCH_STORE_TEST_ALLOW_RESET` to the exact database name; its guarded setup drops the public schema. It proves the non-empty prior-to-current Alembic upgrade, concurrent idempotent ingestion, active derivations, invocation-ledger replacement, run immutability, expired-final-attempt recovery, stale-token rejection, and manifest-definition binding. Record a separate disposable-service acceptance campaign for wrapper preflight/fail-closed behavior, Valkey loss, damaged Qdrant rebuild, activation interruption recovery, cutover, and rollback.

For an authorized live campaign, retain a tagged `research-store-v3` fixture corpus containing unchanged and changed snapshots, semantically overlapping positive controls, an unrelated negative control, one real `fscrape`, one bounded `fsearch`, and one bounded `fsearch_smart` parent batch. Verify stable IDs and full wrapper -> batch -> PostgreSQL/blob -> worker -> Qdrant -> hybrid/reranked passages provenance. Confirm internal smart-search branches do not overwrite one another or persist independently. Restart the worker with queued jobs, exercise a second-index activation/rollback/reactivation, confirm `fr_<uuid>` retrieval events, and prove private mode produces no catalog, database, blob, or Qdrant change.
