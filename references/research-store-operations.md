<!-- @format -->

# Research Store Operations

Compact operator reference. `operations-runbook.md` is normative for recovery and destructive procedures.

## Configure authoritative services

```bash
cd "<skill-root>"
export FIRECRAWL_RESEARCH_PYTHON="<skill-root>/.venv-research-store/bin/python"
source scripts/research-env

scripts/research-db status
scripts/research-db ingest-ready
scripts/research-db doctor
```

Supported acquisition always requires PostgreSQL, a durable writable `BLOB_ROOT`, and a valid acquisition-eligible run before provider execution. No successful acquisition can downgrade to a local-only result.

## Run acquisition

```bash
RUN_ID="$(scripts/frun start 'Research objective')"

scripts/fsearch 'bounded query' \
  --research-run-id "$RUN_ID" \
  --limit 20 \
  --scrape-limit 5

scripts/fscrape 'https://example.com' \
  --research-run-id "$RUN_ID"

scripts/frun finish "$RUN_ID" --outcome satisfied
scripts/frun status "$RUN_ID"
```

`fsearch_smart` creates a run when omitted; `--dry-run` performs planning only.

## Inspect and replay

```bash
scripts/finspect runs --limit 20
scripts/finspect invocations --run "$RUN_ID" --limit 20
scripts/finspect search-responses --run "$RUN_ID" --limit 20
scripts/finspect replay-search '<search-response-uuid>'
scripts/finspect attempts --run "$RUN_ID"
scripts/finspect inspect '<asset-uuid>'
scripts/finspect passages '<asset-uuid>' --max-tokens 2000
```

Select retained candidates by UUID:

```bash
scripts/finspect scrape-candidates '<candidate-uuid>' \
  --idempotency-key '<stable-key>'
```

## Run the lease-safe worker

```bash
scripts/research-db worker \
  --batch-size 32 \
  --poll-seconds 5 \
  --lease-seconds 300 \
  --max-attempts 5
```

Valkey is optional latency optimization; PostgreSQL jobs remain durable.

## Build, activate, and roll back Qdrant

```bash
scripts/research-db index-list
scripts/research-db index-build --current-config --all
scripts/research-db worker --once --batch-size 64
scripts/research-db reconcile-qdrant
scripts/research-db index-activate '<index-id>'
scripts/research-db index-rollback '<prior-index-id>'
scripts/research-db index-prune --dry-run
```

Qdrant is rebuilt from PostgreSQL chunks. Never infer workflow or corpus truth from vectors.

## Rederive and export

```bash
scripts/research-db rederive --snapshot '<snapshot-id>'
scripts/research-db export-invocation 'fc_<uuid>' --output invocation.json
scripts/research-db export-run 'fr_<uuid>' --output run.json
```

Exports are explicit presentation artifacts and are never consumed as workflow, replay, retry, selection, or ingestion state.

## Back up and recover

Capture PostgreSQL and `BLOB_ROOT` at one logical boundary. Restore them together, verify blobs, rebuild Qdrant, recreate Valkey, and restart the worker.

```bash
scripts/research-db verify-blobs
scripts/research-db index-build --current-config --all
scripts/research-db worker --once --batch-size 64
scripts/research-db reconcile-qdrant
scripts/research-db doctor
```

The current Target A keeps payload bytes in `BLOB_ROOT`. Moving them into PostgreSQL is a separate future migration, not an operational mode.
