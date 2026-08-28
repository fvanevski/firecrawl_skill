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

## Run research

Normal retained-first research uses the controller:

```bash
scripts/fresearch run 'Research objective'
scripts/fresearch continue 'fr_<uuid>'
scripts/fresearch result 'fr_<uuid>'
```

The returned typed directive determines whether the same run continues automatically, a durable `oa_<uuid>` human action is required, or the run is terminal. Do not translate the directive into a handcrafted `frun`/`fsearch`/`fscrape` lifecycle.

For explicitly controlled specialist acquisition, prepare the run before provider work:

```bash
RUN_ID="$(scripts/frun start 'Specialist acquisition' --run-mode curated --mode autonomous_local)"
scripts/frun prepare "$RUN_ID"
scripts/fsearch 'bounded query' --research-run-id "$RUN_ID" --limit 20 --scrape-limit 5
```

`research-db worker --once` remains one bounded projection batch, not a complete drain. Specialist completion/curation must follow the current low-level lifecycle contract. The deprecated `fsearch_smart` name is only an exact delegate to `fresearch run`; retired `--dry-run` and spec-skeleton behavior are not current public commands.

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

## Rederive, export, and audit one run offline

```bash
scripts/research-db rederive --snapshot '<snapshot-id>'
scripts/research-db export-invocation 'fc_<uuid>' --output invocation.json
scripts/research-db export-run 'fr_<uuid>' --output run.json
scripts/research-db export-run 'fr_<uuid>' --schema-version export-run-v1 --output legacy-run.json
scripts/research-db integrity 'fr_<uuid>' --output integrity.json
scripts/frun integrity 'fr_<uuid>' --output integrity.json
```

`export-run` defaults to the bounded `export-run-v2` schema; only the explicit compatibility schema `export-run-v1` is also supported. `integrity` emits `integrity-v1`. Unsupported schema labels fail closed.

V2/integrity reads execute inside one read-only repeatable-read PostgreSQL snapshot. Potentially large sections retain exact counts and deterministic hashes while bounding representative records. Secret-bearing values and unsafe user-home paths are recursively redacted across the complete artifact. Integrity stdout is only a bounded write acknowledgement, not a duplicate of the artifact.

Exact indexing evidence is scoped to the persisted run membership/checkpoint and uses PostgreSQL's exact index-job census. Qdrant observations remain projection diagnostics and cannot establish lifecycle or exact-membership truth. Historical terminal census and later job timing are kept distinct; the tooling does not infer unsupported per-job historical provenance.

See `run-integrity-export.md` for the schema, reason codes, bounds, redaction policy, compatibility semantics, audited 1,344+32 scenario, and test matrix.

Exports are explicit presentation/audit artifacts and are never consumed as workflow, replay, retry, selection, ingestion, lifecycle, or completion authority.

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
