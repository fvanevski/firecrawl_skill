# Authoritative `fsearch`

`scripts/fsearch` is the public launcher for a PostgreSQL-authoritative Firecrawl
search. The launcher contains environment bootstrap logic only: it sources
`scripts/research-env` when automatic environment loading is enabled, selects
`FIRECRAWL_RESEARCH_PYTHON`, and executes
`python -m research_store.fsearch_service`.

## Authority and storage

Every invocation requires `DATABASE_URL` for PostgreSQL, an Alembic-head schema,
a writable content-addressed `BLOB_ROOT`, and a nonterminal acquisition-eligible
`fr_<uuid>` research run. The preflight run state and lifecycle revision are
captured before Firecrawl is constructed. The same revision is locked and
revalidated in the transaction that persists the search response, candidates,
and acquisition event. A changed or ineligible run aborts the transaction.

PostgreSQL is authoritative for runs, invocations, search responses, candidate
identities and occurrences, extraction provenance, corpus identities, and index
jobs. `BLOB_ROOT` is the immutable payload authority. Qdrant remains a
rebuildable projection; Valkey notifications are optional and may be lost
without loss of authoritative state.

`fsearch` never writes acquisition state to `TMPDIR`. Search and scrape payloads
are captured through stdout and committed through the authoritative services.
Short-lived secure files used by environment loading or atomic blob writes are
implementation details, not runtime state.

## Idempotency and recovery

A normal invocation receives a search key scoped to its external invocation ID,
so two independent identical queries remain two fresh acquisitions. Supplying
`--idempotency-key` requests replay semantics. The service takes a PostgreSQL
advisory lock, validates the complete stored request envelope, and returns the
committed response and stable candidate IDs before any provider call. Reusing a
key for a different query or search configuration fails.

The invocation-scoped default key also permits recovery after a process failure:
retry the same `--invocation-id` and the committed acquisition is replayed rather
than sent to Firecrawl again. Selected extraction is independently idempotent and
uses stable PostgreSQL candidate IDs, never occurrence IDs, ranks, or paths.

## CLI

```text
fsearch QUERY --research-run-id fr_<uuid> [options]
```

Supported controls include `--limit`, `--scrape-limit`, `--sources`, `--tbs`,
`--profile`, `--idempotency-key`, `--invocation-id`, and `--json`.
`FIRECRAWL_RESEARCH_RUN_ID` and `FIRECRAWL_INVOCATION_ID` provide the two ID
defaults.

The removed `--dir`, `--reuse-search`, and `--scrape-ranks` options fail with
migration guidance. Authoritative PostgreSQL preflight is unconditional.

JSON and console results include run and invocation IDs, search-response ID,
stable candidate IDs, extraction invocation and outcomes, and authoritative
corpus IDs. Lists are bounded and include total-count and truncation fields.
Diagnostics are capped at 500 characters. JSON argument errors are emitted as a
structured `authoritative-fsearch-error-v1` object.

Exit codes are stable by failure stage:

| Code | Stage |
| ---: | --- |
| 2 | preflight or argument contract |
| 3 | search transport |
| 4 | candidate parsing |
| 5 | extraction transport/content failure |
| 6 | PostgreSQL/blob ingestion or idempotency conflict |
| 7 | index manifest or index-job persistence |

A nonzero result may contain a bounded authoritative partial result when the
search committed before a later extraction failure.
