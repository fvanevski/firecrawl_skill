<!-- @format -->

# Research Store Operations

Compact operator reference. `authoritative-workflows.md` is canonical for acquisition, completion, and projection-recovery command ordering. `operations-runbook.md` is normative for recovery and destructive procedures.

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

python3 scripts/drain_index_jobs.py --batch-size 64
scripts/research-db run-status "$RUN_ID"
scripts/frun finish "$RUN_ID" --outcome satisfied
scripts/frun status "$RUN_ID"
```

`research-db worker --once` is one bounded batch, not a complete drain. Do not start another acquisition on the same run or finish it until run-scoped indexing is complete. To add `fscrape` to the same run, drain before and after that operation as shown in `authoritative-workflows.md`.

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

Drain any new index work before another same-run acquisition or completion.

## Run the lease-safe worker

Continuous service:

```bash
scripts/research-db worker \
  --batch-size 32 \
  --poll-seconds 5 \
  --lease-seconds 300 \
  --max-attempts 5
```

Bounded fail-closed drain:

```bash
python3 scripts/drain_index_jobs.py --batch-size 64
```

Valkey is an optional latency optimization; PostgreSQL jobs remain durable.

## Build, activate, and roll back Qdrant

```bash
scripts/research-db index-list
scripts/research-db index-build --current-config --all
python3 scripts/drain_index_jobs.py --batch-size 64
scripts/research-db reconcile-qdrant
scripts/research-db doctor
scripts/research-db index-activate '<index-id>'
scripts/research-db index-rollback '<prior-index-id>'
scripts/research-db index-prune --dry-run
```

Qdrant is rebuilt from PostgreSQL chunks. Never infer workflow or corpus truth from vectors. Activation requires complete PostgreSQL manifests/jobs and a reconciled compatible projection.

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
python3 scripts/drain_index_jobs.py --batch-size 64
scripts/research-db reconcile-qdrant
scripts/research-db doctor
```

The current Target A keeps payload bytes in `BLOB_ROOT`. Moving them into PostgreSQL is a separate future migration, not an operational mode.
