# RC-9 Release Notes: PostgreSQL-Authoritative Runtime

Parent epic: #183
Release issue: #192

## Summary

RC-9 completes the documentation and compatibility boundary for the RC-3 through RC-8 authoritative-storage changes.

The supported runtime now has one acquisition contract:

- PostgreSQL is authoritative for workflow, acquisition provenance, corpus identities, evidence, audits, and durable jobs.
- `BLOB_ROOT` remains the immutable content-addressed store for provider payload bytes under Target A.
- Qdrant remains a rebuildable projection.
- Valkey remains optional transient coordination.
- Supported acquisition fails before Firecrawl or network execution when authoritative preflight fails.
- No successful acquisition can downgrade to scratch-only or file-mediated persistence.

A future migration that stores payload bytes in PostgreSQL is explicitly outside Target A and outside this release.

## Breaking changes

The following runtime surfaces are removed:

- `scripts/persist_results.py`
- `scripts/fread`
- `research-db import-scratch`
- `SCRATCH_ROOT`
- `FIRECRAWL_RESEARCH_PERSIST=auto|on|off`
- `FIRECRAWL_RESEARCH_ACTIVE`
- `FIRECRAWL_CAPTURE_RAW`
- file-mediated completion or ingestion through `_corpus.json`
- acquisition-state directories such as `firecrawl_scratch`
- `fsearch --reuse-search`
- `fsearch --scrape-ranks`
- `fsearch --dir`
- `fscrape --output-dir`

The current runtime does not create or consume `_search.json`, `_meta.json`, `_index.md`, `_workflow_input.json`, `_raw`, `result_*`, or `url_*` acquisition state.

## Parser and error compatibility

- `fsearch --dir` remains a hidden compatibility tombstone and returns targeted migration guidance before service construction.
- `fsearch --reuse-search` and `fsearch --scrape-ranks` are not registered. `argparse` returns the standard `unrecognized arguments` error before service construction.
- `fscrape --output-dir PATH` and `fscrape --output-dir=PATH` return targeted migration guidance before parser/service execution.
- Missing PostgreSQL configuration or an invalid/missing run fails authoritative preflight and permits zero provider/network calls.

## Current equivalents

| Former workflow | Current workflow |
|---|---|
| list local history | `finspect runs`, `invocations`, and `search-responses` |
| reuse a retained search | `finspect replay-search <search-response-id>` |
| scrape selected ranks | `finspect scrape-candidates <candidate-id>...` |
| retry failed selected items | `finspect retry-candidates <prior-invocation-id> --idempotency-key <new-key>` |
| read a local result | `finspect inspect <asset-id>` and `passages <asset-id>` |
| grep local acquisitions | `finspect lexical-search` or bounded `pattern-search` |
| choose an acquisition destination | no runtime destination; authoritative services select PostgreSQL and `BLOB_ROOT` |
| produce portable JSON | `research-db export-invocation` or `export-run` |

Explicit exports are presentation artifacts and are never consumed as workflow, replay, retry, selection, or ingestion state.

## Required run binding

`fsearch` and `fscrape` require a valid acquisition-eligible `fr_<uuid>` through `--research-run-id` or `FIRECRAWL_RESEARCH_RUN_ID`.

```bash
RUN_ID="$(scripts/frun start 'Research objective')"
scripts/fsearch 'query' --research-run-id "$RUN_ID"
scripts/fscrape 'https://example.com' --research-run-id "$RUN_ID"
```

`fsearch_smart` creates an authoritative run when no run ID is supplied. `fsearch_smart --dry-run` is planning-only and performs no database or network writes.

## Legacy acquisition-tree migration

The exact last main revision that still contains `research-db import-scratch` is:

```text
82d3369c0be9bba381f38b598c3b05ed4b683ae6
```

The removal landed in RC-6 merge commit:

```text
1aaa92f7c3a84ea1ed210947130b120cc814826e
```

No tag name is asserted. Use the exact compatibility commit in an isolated worktree, import into a clean supported PostgreSQL/`BLOB_ROOT` deployment, verify identities and blobs, then deploy the current release. See `migration-guide.md`.

Old trees that are not imported before upgrade require that exact compatibility checkout or an external one-shot migration tool. The current runtime intentionally does not retain an importer.

## Migration effects

- The current clean schema head remains `0038_postgres_authority`.
- Current supported PostgreSQL deployments use normal forward migration.
- Databases from an unsupported older lineage must not be manually stamped.
- Payload bytes remain in `BLOB_ROOT`; no payload-byte migration occurs.
- Qdrant can be rebuilt after upgrade.
- Valkey data does not require migration.

## Rollback

Before upgrade, capture a matching PostgreSQL dump and `BLOB_ROOT` backup plus active Qdrant metadata.

To roll back:

1. stop writers and workers;
2. restore PostgreSQL and `BLOB_ROOT` from the same boundary;
3. deploy code compatible with that boundary;
4. rebuild or reactivate Qdrant;
5. recreate Valkey;
6. run `verify-blobs`, `status`, `ingest-ready`, and `doctor`.

Do not restore workflow truth from explicit exports, Qdrant, Valkey, or a legacy acquisition tree.

## Validation gate

Release acceptance requires:

- Ruff lint and formatting;
- parser-backed documentation tests;
- zero stale operational references;
- Python 3.11 and 3.12;
- disposable PostgreSQL, Qdrant, and Valkey;
- provider-call suppression after failed preflight;
- idempotent search/scrape replay;
- blob integrity;
- worker restart and lease recovery;
- Qdrant rebuild and rollback;
- Valkey-loss tolerance;
- explicit-export reproducibility;
- exact tested commit SHA evidence.
