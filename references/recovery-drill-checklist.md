# Recovery Drill Checklist

Run these drills quarterly, after infrastructure changes, and before a release that changes persistence or recovery contracts. Record the exact candidate SHA and environment.

## Prerequisites

- [ ] Disposable or approved staging PostgreSQL
- [ ] Matching `BLOB_ROOT`
- [ ] Qdrant
- [ ] Optional Valkey
- [ ] Embedding and reranking endpoints
- [ ] Firecrawl endpoint and credentials
- [ ] Worker service access
- [ ] Current backup and rollback plan

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
| 7 | Verify bytes and schema | `rtk proxy "<skill-root>/scripts/research-db" verify-blobs` and `status` | [ ] |
| 8 | Build projection | `rtk proxy "<skill-root>/scripts/research-db" index-build --current-config --all` | [ ] |
| 9 | Drain worker | `rtk proxy "<skill-root>/scripts/research-db" worker --once --batch-size 64` | [ ] |
| 10 | Reconcile and activate | `reconcile-qdrant`, then `index-activate "<index-id>"` | [ ] |
| 11 | Recreate Valkey and worker | service procedure | [ ] |
| 12 | Verify health | `rtk proxy "<skill-root>/scripts/research-db" doctor` | [ ] |
| 13 | Start a run | `RUN_ID="$(rtk proxy "<skill-root>/scripts/frun" start 'Recovery drill acquisition')"` | [ ] |
| 14 | Test acquisition | `rtk proxy "<skill-root>/scripts/fsearch" "test query" --research-run-id "$RUN_ID" --limit 5 --scrape-limit 2` | [ ] |
| 15 | Resolve provenance | inspect stable response, candidate, snapshot, document, chunk, blob, and job IDs | [ ] |

Pass only when the restored PostgreSQL records resolve to matching verified blob bytes and compatible Qdrant points.

## Drill 2: Index Cutover Recovery

**Objective:** prove interrupted projection replacement can complete or roll back without modifying authoritative corpus data.

| Step | Action | Command / evidence | Status |
|---|---|---|---|
| 1 | Record active index | `index-list` and `doctor` | [ ] |
| 2 | Build replacement | `index-build --current-config --all` | [ ] |
| 3 | Interrupt before activation | controlled staging interruption | [ ] |
| 4 | Reconcile | `reconcile-qdrant` | [ ] |
| 5 | Complete activation | `index-activate "<index-id>"` | [ ] |
| 6 | Verify retrieval | active fingerprint and bounded known passage | [ ] |
| 7 | Roll back | `index-rollback "<prior-index-id>"` | [ ] |
| 8 | Verify prior alias | `doctor` | [ ] |

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
| 7 | Drain jobs and finish | `worker --once`, `doctor`, `frun finish` | [ ] |

Repeat with a failed preflight and prove the Firecrawl invocation count remains zero.

## Drill 4: Endpoint Failure

**Objective:** verify endpoint failures remain explicit and recoverable.

| Step | Action | Command / evidence | Status |
|---|---|---|---|
| 1 | Stop embedding or reranking endpoint | controlled staging outage | [ ] |
| 2 | Observe failure | `endpoint-health`, `resource-status`, `doctor` | [ ] |
| 3 | Verify no silent substitute | recorded degraded or failed status | [ ] |
| 4 | Restart with identical model identity | service procedure | [ ] |
| 5 | Drain jobs | `worker --once --batch-size 32` | [ ] |
| 6 | Verify completion | `doctor` and run status | [ ] |

## Drill 5: Valkey Loss

**Objective:** prove durable jobs continue from PostgreSQL polling.

| Step | Action | Command / evidence | Status |
|---|---|---|---|
| 1 | Queue authoritative index work | bounded test acquisition | [ ] |
| 2 | Stop Valkey before notification consumption | controlled outage | [ ] |
| 3 | Keep worker polling PostgreSQL | worker log and job state | [ ] |
| 4 | Verify completion | manifests and Qdrant points complete | [ ] |
| 5 | Restart Valkey | service procedure | [ ] |
| 6 | Verify no data repair | `doctor` | [ ] |

## Drill 6: Migration Upgrade

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
- Valkey and worker observations;
- provider invocation counts for failed preflight and retry cases;
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
Results:
Failures:
Remediation:
Residual risk:
Approver:
```
