# Recovery Drill Checklist

Run these drills quarterly, after infrastructure changes, and before a release that changes persistence or recovery contracts. Record the exact candidate SHA and environment. `authoritative-workflows.md` is canonical for transaction, acquisition, completion, and projection-recovery ordering.

## Prerequisites

- [ ] Disposable or approved staging PostgreSQL
- [ ] Matching `BLOB_ROOT`
- [ ] Qdrant
- [ ] Optional Valkey
- [ ] Embedding and reranking endpoints
- [ ] Firecrawl endpoint and credentials
- [ ] Worker service access
- [ ] Current backup and rollback plan

`research-db worker --once` processes at most one bounded batch. Every step labeled “drain” below means:

```bash
rtk proxy python3 "<skill-root>/scripts/drain_index_jobs.py" --batch-size 64
```

The step passes only after the helper returns zero following a batch with `claimed=0`. Any failed or lease-lost work is a drill failure requiring diagnosis.

## Drill 1: Full Disaster Recovery

**Objective:** restore the Target A authority boundary after complete service loss.

| Step | Action | Command / evidence | Status |
|---|---|---|---|
| 1 | Stop writers and worker | `systemctl --user stop firecrawl-research-indexer.service` | [ ] |
| 2 | Capture PostgreSQL | `pg_dump --format=custom --file=/tmp/research-drill.dump "$DATABASE_URL"` | [ ] |
| 3 | Capture blob inventory | `find "$BLOB_ROOT" -type f -print0 \| sort -z \| xargs -0 sha256sum > /tmp/blob-inventory.sha256` | [ ] |
| 4 | Record projection | `rtk proxy "<skill-root>/scripts/research-db" index-list` | [ ] |
| 5 | Simulate approved loss | disposable services only | [ ] |
| 6 | Restore PostgreSQL and matching blob root | backup procedure | [ ] |
| 7 | Verify bytes and schema | `verify-blobs`, `status`, and matching inventory | [ ] |
| 8 | Build projection | `index-build --current-config --all` | [ ] |
| 9 | Drain all durable jobs | `drain_index_jobs.py --batch-size 64`; retain every JSON batch result | [ ] |
| 10 | Reconcile | `reconcile-qdrant`; require zero missing and orphaned expected points | [ ] |
| 11 | Activate | `index-activate "<index-id>"` only after complete manifests and compatible fingerprint | [ ] |
| 12 | Recreate Valkey and worker | service procedure | [ ] |
| 13 | Verify health | `doctor` and active-alias fingerprint | [ ] |
| 14 | Start a run | `RUN_ID="$(rtk proxy "<skill-root>/scripts/frun" start 'Recovery drill acquisition')"` | [ ] |
| 15 | Test acquisition | `fsearch "test query" --research-run-id "$RUN_ID" --limit 5 --scrape-limit 2` | [ ] |
| 16 | Drain run jobs | `drain_index_jobs.py --batch-size 64` | [ ] |
| 17 | Verify and finish | `run-status`, then `frun finish` and `frun status` | [ ] |
| 18 | Resolve provenance | stable response, candidate, snapshot, document, chunk, blob, and job IDs | [ ] |

Pass only when restored PostgreSQL records resolve to matching verified blob bytes and compatible reconciled Qdrant points.

## Drill 2: Index Cutover Recovery

**Objective:** prove interrupted projection replacement can complete or roll back without modifying authoritative corpus data.

| Step | Action | Command / evidence | Status |
|---|---|---|---|
| 1 | Record active index | `index-list` and `doctor` | [ ] |
| 2 | Build replacement | `index-build --current-config --all` | [ ] |
| 3 | Process a bounded batch | one explicit `worker --once --batch-size 64`; prove work remains when the dataset exceeds the batch | [ ] |
| 4 | Interrupt before completion | controlled staging interruption | [ ] |
| 5 | Resume and drain | `drain_index_jobs.py --batch-size 64`; retain all batch results | [ ] |
| 6 | Reconcile | `reconcile-qdrant`; require zero missing and orphaned expected points | [ ] |
| 7 | Complete activation | `index-activate "<index-id>"` | [ ] |
| 8 | Verify retrieval | active fingerprint and bounded known passage | [ ] |
| 9 | Roll back | `index-rollback "<prior-index-id>"` | [ ] |
| 10 | Verify prior alias | `doctor` and bounded retrieval | [ ] |

The drill is invalid if it activates without proving all PostgreSQL manifests/jobs complete after the interruption.

## Drill 3: Run Recovery

**Objective:** recover an interrupted authoritative operation without duplicate provider work or ledger edits.

| Step | Action | Command / evidence | Status |
|---|---|---|---|
| 1 | Start run | `RUN_ID="$(rtk proxy "<skill-root>/scripts/frun" start 'Run recovery drill')"` | [ ] |
| 2 | Start acquisition with stable key | `fsearch ... --research-run-id "$RUN_ID" --idempotency-key drill-key --invocation-id fc_<uuid>` | [ ] |
| 3 | Interrupt after provider response at a controlled test hook | disposable environment | [ ] |
| 4 | Inspect run and invocation | `run-status`, `finspect invocations --run "$RUN_ID"` | [ ] |
| 5 | Repeat identical input and identity | same idempotency key and invocation ID | [ ] |
| 6 | Verify replay | no duplicate Firecrawl invocation; same authoritative IDs | [ ] |
| 7 | Drain all jobs | `drain_index_jobs.py --batch-size 64` | [ ] |
| 8 | Verify completion boundary | `run-status` shows no unfinished/dead run-scoped indexing | [ ] |
| 9 | Finish | `frun finish`, then `frun status` | [ ] |

Repeat with a failed preflight and prove the Firecrawl invocation count remains zero. Also prove that attempting a second acquisition or `frun finish` before step 7 fails closed.

## Drill 4: Endpoint Failure

**Objective:** verify endpoint failures remain explicit and recoverable.

| Step | Action | Command / evidence | Status |
|---|---|---|---|
| 1 | Stop embedding or reranking endpoint | controlled staging outage | [ ] |
| 2 | Observe failure | `endpoint-health`, `resource-status`, `doctor` | [ ] |
| 3 | Verify no silent substitute | recorded degraded or failed status | [ ] |
| 4 | Restart with identical model identity | service procedure | [ ] |
| 5 | Drain retryable jobs | `drain_index_jobs.py --batch-size 32` | [ ] |
| 6 | Verify completion | no failed/dead/missing jobs; `doctor` and run status | [ ] |

## Drill 5: Valkey Loss

**Objective:** prove durable jobs continue from PostgreSQL polling.

| Step | Action | Command / evidence | Status |
|---|---|---|---|
| 1 | Queue authoritative index work | bounded test acquisition | [ ] |
| 2 | Stop Valkey before notification consumption | controlled outage | [ ] |
| 3 | Drain through PostgreSQL polling | `drain_index_jobs.py --batch-size 64` with Valkey unavailable | [ ] |
| 4 | Verify completion | manifests and Qdrant points complete | [ ] |
| 5 | Restart Valkey | service procedure | [ ] |
| 6 | Verify no data repair | `doctor` and unchanged authoritative IDs | [ ] |

## Drill 6: Blob and Metadata Crash Windows

**Objective:** prove the documented Target A transaction order.

| Step | Action | Command / evidence | Status |
|---|---|---|---|
| 1 | Inject a blob-write failure | controlled store fault before metadata persistence | [ ] |
| 2 | Verify suppression | no committed snapshot, document, chunk, run-asset, manifest, or job row | [ ] |
| 3 | Inject a PostgreSQL failure after durable blob installation | controlled transaction fault | [ ] |
| 4 | Verify orphan-only outcome | unreferenced digest may remain; no committed metadata references it | [ ] |
| 5 | Run integrity checks | `verify-blobs` and bounded orphan inventory | [ ] |
| 6 | Complete a normal acquisition | returned IDs resolve to committed rows and matching bytes | [ ] |

Committed metadata that points to absent or digest-mismatched bytes is a blocking failure.

## Drill 7: Migration Upgrade

**Objective:** validate the current supported migration and legacy-tree boundary.

1. For a current supported PostgreSQL database, back up PostgreSQL and `BLOB_ROOT`, then run `migrate`, `status`, `ingest-ready`, and `doctor`.
2. For an unimported legacy acquisition tree, use the exact compatibility revision documented in `migration-guide.md` before deploying the current release.
3. Verify stable imported source, snapshot, document, and chunk identities and matching blobs.
4. Deploy the current candidate and rerun health checks.
5. Exercise `finspect` history, replay, candidate selection, and bounded passages.
6. Restore the pre-upgrade backup in a disposable environment and prove the rollback procedure.

Do not claim an in-place upgrade for an unsupported older database lineage.

## Post-drill report

Record:

- date, operator, environment, and exact commit SHA;
- drill steps and commands;
- PostgreSQL and blob backup identifiers;
- active and rollback Qdrant fingerprints;
- every worker-drain JSON result;
- Valkey and worker observations;
- provider invocation counts for failed preflight and retry cases;
- blob-write and metadata-rollback fault evidence;
- failures, remediation, residual risk, and sign-off.

```text
Drill Report
============
Date:
Candidate SHA:
Environment:
Drills:
PostgreSQL backup:
Blob backup:
Active index:
Rollback index:
Worker drain evidence:
Results:
Failures:
Remediation:
Residual risk:
Approver:
```
