# Canonical Authoritative Workflows

This file is the canonical source for release-facing acquisition, completion, and projection-recovery command sequences. Other operational documents should link here rather than inventing a different lifecycle.

## Transaction invariant

Target A uses this ordering for acquired payloads:

1. authoritative preflight validates PostgreSQL, schema, privileges, `BLOB_ROOT`, and run eligibility before Firecrawl or network execution;
2. provider payload bytes are durably installed in `BLOB_ROOT` by digest;
3. PostgreSQL commits the invocation, provenance, snapshot/document/chunk identities, run links, embedding manifests, and durable index jobs that reference the installed digest;
4. stable authoritative IDs are returned only after the PostgreSQL commit succeeds.

A PostgreSQL rollback after the blob write may leave an unreferenced orphan blob. It must not leave committed metadata pointing to absent bytes. Orphans are reportable bounded-cleanup candidates, never corpus records.

## Drain durable index jobs

`research-db worker --once` processes at most one bounded batch. It is not a queue-drain command. Use the fail-closed helper whenever a procedure must prove that no claimable PostgreSQL jobs remain:

```bash
python3 scripts/drain_index_jobs.py --batch-size 64
```

The helper repeatedly runs `research-db worker --once`, prints every versioned JSON result, and returns success only after a batch reports `claimed=0`. It returns nonzero for invalid output, a worker error, any reported failed or lease-lost work, or an exceeded batch bound.

A continuously running worker service is also valid, but the operator must still verify run-scoped completion before starting additional acquisition on a run, finishing a run, reconciling Qdrant, or activating an index.

## Start, acquire, index, and finish one run

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

Do not issue another `fsearch`, `fscrape`, or candidate-acquisition command on the same run while its index work is unfinished. To add a direct scrape to the same run, first drain and verify the prior jobs, then acquire and drain again:

```bash
python3 scripts/drain_index_jobs.py --batch-size 64

scripts/fscrape 'https://example.com/article' \
  --research-run-id "$RUN_ID"

python3 scripts/drain_index_jobs.py --batch-size 64
scripts/research-db run-status "$RUN_ID"
```

## Rebuild and activate Qdrant

```bash
scripts/research-db index-list
scripts/research-db index-build --current-config --all
python3 scripts/drain_index_jobs.py --batch-size 64
scripts/research-db reconcile-qdrant
scripts/research-db doctor
scripts/research-db index-activate '<index-id>'
```

Activation is valid only after PostgreSQL manifests and jobs are complete, Qdrant has zero missing or orphaned expected points, the schema and embedding fingerprint are compatible, and the probe succeeds. Preserve the prior collection for rollback.

## Recovery evidence

For every acquisition, completion, restore, or rebuild procedure, retain:

- exact code SHA and configuration fingerprint;
- authoritative run and invocation IDs;
- each worker-drain JSON result;
- final PostgreSQL run/job state;
- blob verification result;
- Qdrant reconciliation and active-alias state;
- any failure, retry, or orphan-cleanup decision.
