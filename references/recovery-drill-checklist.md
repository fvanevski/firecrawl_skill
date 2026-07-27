# Recovery Drill Checklist

Periodic checklist for verifying that recovery procedures work end-to-end.
Run these drills quarterly or after any infrastructure change.

## Prerequisites

- [ ] Access to the production or staging PostgreSQL instance
- [ ] Access to the blob root directory
- [ ] Access to the Qdrant instance
- [ ] Access to the Valkey instance (optional — loss is safe)
- [ ] Local model endpoints available (LLM, embedding, reranker)
- [ ] `DATABASE_URL` and `BLOB_ROOT` configured
- [ ] Worker service accessible (`systemctl --user`)

---

## Drill 1: Full Disaster Recovery

**Objective:** Verify that the system can be fully restored from backup after complete data loss.

**Duration estimate:** 30–60 minutes

| Step | Action | Command / Procedure | Status |
|------|--------|---------------------|--------|
| 1 | Capture a consistent backup | `pg_dump --format=custom --file=/tmp/pre-drill-backup.dump "$DATABASE_URL"` | [ ] |
| 2 | Capture blob inventory | `find "$BLOB_ROOT" -type f -exec sha256sum {} + > /tmp/blob-inventory.txt` | [ ] |
| 3 | Record Qdrant state | `rtk proxy "<skill-root>/scripts/research-db" index-list` | [ ] |
| 4 | Stop all services | `systemctl --user stop firecrawl-research-indexer.service` | [ ] |
| 5 | Simulate data loss | Drop the database or rename blob root | [ ] |
| 6 | Restore PostgreSQL | `pg_restore --dbname="$DATABASE_URL" --clean --if-exists /tmp/pre-drill-backup.dump` | [ ] |
| 7 | Restore blob root | Restore from backup or recreate from source | [ ] |
| 8 | Verify blob integrity | `rtk proxy "<skill-root>/scripts/research-db" verify-blobs` | [ ] |
| 9 | Rebuild the index | `rtk proxy "<skill-root>/scripts/research-db" index-build --current-config --all` | [ ] |
| 10 | Drain the worker | `rtk proxy "<skill-root>/scripts/research-db" worker --once --batch-size 64` | [ ] |
| 11 | Reconcile Qdrant | `rtk proxy "<skill-root>/scripts/research-db" reconcile-qdrant` | [ ] |
| 12 | Activate the index | `rtk proxy "<skill-root>/scripts/research-db" index-activate "<index-id>"` | [ ] |
| 13 | Verify with doctor | `rtk proxy "<skill-root>/scripts/research-db" doctor` — all components healthy | [ ] |
| 14 | Restart the worker | `systemctl --user start firecrawl-research-indexer.service` | [ ] |
| 15 | Run a test acquisition | `rtk proxy "<skill-root>/scripts/fsearch" "test query" --limit 5 --scrape-limit 2` | [ ] |
| 16 | Verify end-to-end provenance | Check that the test acquisition appears in PostgreSQL, blob, and Qdrant | [ ] |

---

## Drill 2: Index Cutover Recovery

**Objective:** Verify that an interrupted index cutover can be completed or rolled back.

**Duration estimate:** 10–15 minutes

| Step | Action | Command / Procedure | Status |
|------|--------|---------------------|--------|
| 1 | Build a new index | `rtk proxy "<skill-root>/scripts/research-db" index-build --current-config --all` | [ ] |
| 2 | Simulate interruption | Stop worker mid-activation (kill the worker process) | [ ] |
| 3 | Check interrupted state | `rtk proxy "<skill-root>/scripts/research-db" index-list` — verify prepared/switched state | [ ] |
| 4 | Complete the activation | `rtk proxy "<skill-root>/scripts/research-db" index-activate "<index-id>"` | [ ] |
| 5 | Verify active alias | `rtk proxy "<skill-root>/scripts/research-db" doctor` — active fingerprint matches | [ ] |

**Alternative path (rollback instead of complete):**

| Step | Action | Command / Procedure | Status |
|------|--------|---------------------|--------|
| 4b | Rollback to prior index | `rtk proxy "<skill-root>/scripts/research-db" index-rollback "<prior-index-id>"` | [ ] |
| 5b | Verify prior alias | `rtk proxy "<skill-root>/scripts/research-db" doctor` — prior fingerprint active | [ ] |

---

## Drill 3: Run Recovery

**Objective:** Verify that an interrupted research run can be recovered.

**Duration estimate:** 5–10 minutes

| Step | Action | Command / Procedure | Status |
|------|--------|---------------------|--------|
| 1 | Start a test run | `RUN_ID="$(rtk proxy "<skill-root>/scripts/frun" start 'Test recovery' --profile auto)"` | [ ] |
| 2 | Transition to a non-terminal state | `rtk proxy "<skill-root>/scripts/research-db" run-transition "$RUN_ID" acquiring --expected-revision 1 --idempotency-key "drill-transition"` | [ ] |
| 3 | Simulate interruption | Kill the process that was executing the transition | [ ] |
| 4 | Check interrupted state | `rtk proxy "<skill-root>/scripts/research-db" run-status "$RUN_ID"` | [ ] |
| 5 | Reopen the run | `rtk proxy "<skill-root>/scripts/research-db" run-reopen "$RUN_ID" --reason "drill recovery"` | [ ] |
| 6 | Resume work and complete | Finish the run normally | [ ] |

---

## Drill 4: Endpoint Failure

**Objective:** Verify that endpoint failures are recorded and recoverable.

**Duration estimate:** 10–15 minutes

| Step | Action | Command / Procedure | Status |
|------|--------|---------------------|--------|
| 1 | Stop the local LLM endpoint | Kill the vLLM or LiteLLM process | [ ] |
| 2 | Start an autonomous_local run | `RUN_ID="$(rtk proxy "<skill-root>/scripts/frun" start 'Endpoint failure test' --profile auto)"` then `fsearch_smart "test"` | [ ] |
| 3 | Verify failure is recorded | Check `doctor` and run-status for recorded failures | [ ] |
| 4 | Restart the endpoint | Start the vLLM or LiteLLM process | [ ] |
| 5 | Verify worker retries | `rtk proxy "<skill-root>/scripts/research-db" worker --once --batch-size 32` | [ ] |
| 6 | Verify completion | `rtk proxy "<skill-root>/scripts/research-db" run-status "$RUN_ID"` | [ ] |

---

## Drill 5: Migration Upgrade

**Objective:** Verify that migrations upgrade cleanly on a populated database.

**Duration estimate:** 15–30 minutes

| Step | Action | Command / Procedure | Status |
|------|--------|---------------------|--------|
| 1 | Create a disposable database | `createdb firecrawl_drill_migration` | [ ] |
| 2 | Set DATABASE_URL | `export DATABASE_URL='postgresql://...@localhost/firecrawl_drill_migration'` | [ ] |
| 3 | Import existing data | `rtk proxy "<skill-root>/scripts/research-db" import-scratch "$SCRATCH_ROOT"` | [ ] |
| 4 | Run migrations | `rtk proxy "<skill-root>/scripts/research-db" migrate` | [ ] |
| 5 | Verify schema | `rtk proxy "<skill-root>/scripts/research-db" status` — current = head | [ ] |
| 6 | Verify corpus data | `rtk proxy "<skill-root>/scripts/research-db" corpus-overview` — data preserved | [ ] |
| 7 | Verify blob integrity | `rtk proxy "<skill-root>/scripts/research-db" verify-blobs` — all hashes valid | [ ] |
| 8 | Run integration tests | `pytest -q -p no:cacheprovider "<skill-root>/scripts/test_research_store_integration.py"` | [ ] |

---

## Post-Drill Report

After each drill, record:

- Date and time
- Drill number(s) performed
- Duration
- Any failures or anomalies
- Remediation actions taken
- Follow-up items

```
Drill Report
============
Date: YYYY-MM-DD
Drills: 1, 2, 3, 4, 5
Duration: XX minutes
Failures: None / List failures
Anomalies: List any unexpected behavior
Remediation: List actions taken
Follow-up: List items requiring attention
```

---

*This checklist is derived from `references/operations-runbook.md` Section 14. Update it when new failure modes are discovered or new procedures are validated.*
