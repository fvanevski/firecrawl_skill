# Authoritative `fscrape`

`scripts/fscrape` is the public launcher for direct URL acquisition. It contains
environment bootstrap logic only and executes
`python -m research_store.fscrape_cli`.

## Authority boundary

Every invocation requires:

- an authoritative PostgreSQL connection at Alembic head;
- a writable content-addressed `BLOB_ROOT`;
- a nonterminal acquisition-eligible `fr_<uuid>` research run; and
- the direct-scrape table privileges verified by the shared preflight.

PostgreSQL remains authoritative for run, invocation, candidate, extraction,
provenance, corpus, and index-job identities. `BLOB_ROOT` retains immutable
provider bytes. Qdrant is a rebuildable projection, and Valkey remains optional
transient coordination. Payload bytes are not stored in PostgreSQL.

The CLI does not create `url_*`, `_meta.json`, `_index.md`,
`_workflow_input.json`, `_raw`, or `_corpus.json`. It does not use `TMPDIR` for
acquisition state. Explicit export is a separate database-native operation.

## Execution and failure ordering

The CLI validates its argument contract and JSON Schema, constructs the service,
and then delegates to `DirectScrapeService`. The shared authoritative preflight
and direct-persistence privilege check complete before the Firecrawl adapter is
constructed or invoked. A failed preflight therefore produces no network call.

Provider output is captured from stdout in memory. Structured JSON output is
validated against the supplied Draft 2020-12 schema before corpus ingestion. An
invalid provider payload is retained in `BLOB_ROOT` and recorded as a failed
extraction attempt; it is not committed as a successful document.

After ingestion commits, `fscrape` reads the PostgreSQL `index_jobs` rows for the
committed chunk IDs. Missing index jobs are an indexing failure; the command does
not report a successful authoritative acquisition with incomplete projection
work.

Before returning a result, `fscrape` reads the committed
`research_invocations.external_invocation_id`. The result therefore reports the
PostgreSQL identity rather than independently echoing the current request. This
also applies to explicit-key replay: if a later caller supplies a different
`--invocation-id` with an already committed `--idempotency-key`, the replayed
result reports the original committed invocation identity.

## CLI

```text
fscrape URL [URL ...] --research-run-id fr_<uuid> [options]
```

Supported options:

- `--format markdown|html|rawHtml|json|links|images|summary`
- `--summary` / `-S`
- `--schema '<json-schema>'`
- `--schema-file PATH`
- `--invocation-id fc_<uuid>`
- `--idempotency-key KEY`
- `--json`

`FIRECRAWL_RESEARCH_RUN_ID` and `FIRECRAWL_INVOCATION_ID` provide ID defaults.
`FIRECRAWL_RESEARCH_PERSIST=off` is rejected. Both
`--output-dir PATH` and `--output-dir=PATH` fail with migration guidance to
separate database-native export tooling.

All parser and argument failures, including unsupported formats, use the same
preflight error contract and exit code. When `--json` is present, errors are
written as bounded JSON to stdout; otherwise they are written to stderr.

Normal calls receive an idempotency key scoped to the external invocation ID.
Retrying the same invocation replays the committed batch without another
provider call. An explicit `--idempotency-key` requests caller-controlled replay
semantics, while PostgreSQL remains authoritative for the identity returned.

## Result contract

JSON output contains `schema_version: authoritative-fscrape-v1` and bounded lists
with total counts and truncation flags. It reports:

- internal run, batch/invocation, and committed external invocation IDs;
- per-URL success or failure in original request order;
- candidate and extraction-attempt IDs;
- source, snapshot, document, derivation, and chunk IDs;
- index-job IDs for committed chunks;
- format, MIME type, content/blob digests, and reuse flags; and
- bounded diagnostics for failed items.

No successful result contains a local acquisition path. A partial batch is
returned with its committed authoritative identities and a nonzero extraction
exit status.

Exit codes:

| Code | Stage |
| ---: | --- |
| 2 | argument or authoritative preflight |
| 5 | one or more extraction items failed |
| 6 | PostgreSQL/blob ingestion or idempotency failure |
| 7 | index-job persistence or lookup failure |
