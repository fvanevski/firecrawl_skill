# Legacy entry-point adapters

Issue `P1-08` adds a compatibility boundary; it does not implement the later
planning, acquisition, coverage, extraction, evidence, or synthesis services.

## Feature mode

Set `FIRECRAWL_LEGACY_ADAPTER_MODE` to one of:

- `compatibility` (default): execute the existing wrapper path. The adapter
  performs no database work, so existing commands, flags, output, Catalog
  artifacts, and corpus persistence behavior are unchanged.
- `shadow`: execute the existing wrapper path and append a comparison between
  the legacy decision and its proposed service operation. The proposal does not
  create invocations, append workflow events, transition the run, or change its
  lifecycle revision.
- `authoritative`: require an existing `--research-run-id` for search and scrape
  wrappers, record the routed invocation and event through the PostgreSQL
  workflow repository, and retain the legacy artifacts as compatibility output.

Shadow and authoritative modes require `DATABASE_URL` and a schema at the
current Alembic head. Adapter validation and persistence errors are returned to
the legacy caller; there is no silent fallback to compatibility mode.

Query all comparisons or only divergences with:

```bash
rtk proxy "<skill-root>/scripts/research-db" legacy-comparisons \
  --research-run-id "$RUN_ID"
rtk proxy "<skill-root>/scripts/research-db" legacy-comparisons \
  --divergent-only --entry-point fsearch_smart
```

## Deprecation map

| Legacy path | Current adapter operation | Compatibility status | Later owner (not implemented here) |
| --- | --- | --- | --- |
| `frun` lifecycle commands | `research_run.lifecycle` | Retained; existing `ResearchRunService` routing remains authoritative when persistence is active | consolidated `research run` CLI |
| `fsearch_smart` | `research_workflow.orchestrate` | Retained; only the completed legacy decision is compared/routed | planning, acquisition, coverage, and extraction services |
| `fsearch` | `acquisition.single_query` | Retained; Firecrawl execution and flags are unchanged | `AcquisitionService` |
| `fscrape` | `extraction.batch` | Retained; Firecrawl execution and flags are unchanged | `ExtractionService` |
| `research-db` | unchanged | Not deprecated in Phase 1 | future consolidated administration CLI |

The operation names are routing contracts, not substitute implementations of
the later services.

## Rollback and repair

To roll back orchestration behavior, set the adapter mode to `compatibility`.
Do not remove migration `0008` or delete comparison history. If a comparison
write fails, correct database availability or schema state and retry the same
wrapper command with the same invocation and idempotency identity where the
legacy command supports replay. If committed authoritative records disagree
with a compatibility artifact, retain both, query the comparison ledger, and
apply a forward repair; never rewrite append-only rows.
