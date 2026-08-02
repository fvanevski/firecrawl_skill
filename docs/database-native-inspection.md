# Database-native history, replay, and inspection

`finspect` replaces operational inspection of acquisition directories with bounded reads from PostgreSQL and verified immutable payload reads from `BLOB_ROOT`.

## Authority boundary

- PostgreSQL is authoritative for runs, invocations, search responses, candidates, extraction attempts, provenance, corpus identities, and index jobs.
- `BLOB_ROOT` retains immutable provider payload bytes. `replay-search` verifies both stored SHA-256 and byte length before returning a payload.
- Qdrant remains a rebuildable projection and is not consulted by these commands.
- Valkey is not required for history, replay, inspection, or correctness.
- Candidate acquisition accepts stable candidate UUIDs only and delegates to the authoritative direct-scrape service. Database, run, blob-store, schema, and privilege preflight completes before a Firecrawl adapter is constructed or invoked.
- Explicit user-requested exports remain presentation artifacts. They are never consumed as runtime replay, selection, retry, or ingestion state.

No command in this surface accepts a scratch directory, result rank, local payload path, `_corpus.json`, or file-mediated ingestion manifest.

## Public output and cursor contract

All commands emit versioned JSON. List, passage, lexical-search, and pattern-search cursors are opaque, versioned, and bound to the original operation and scope. A cursor from another run, candidate, asset, query, pattern, or search mode is rejected rather than silently applied to a different result set.

Public limits are fail-closed:

| Surface | Limit |
|---|---:|
| List page | 100 records |
| Passage/search page | 100 records |
| Passage/search text | 64,000 characters |
| Passage/search tokens | 16,000 actual tokenizer-counted tokens |
| Search replay payload | 4 MiB maximum; 1 MiB default |
| Candidate acquisition batch | 20 candidate IDs |
| Returned identity array | 100 IDs, with total and truncation metadata |
| Nested metadata/JSON preview | 8,000 serialized characters |

If a text bound falls within a chunk, the cursor records an intra-chunk offset. Continuing with `next_cursor` returns the exact unreturned suffix; concatenating all pages reproduces the original persisted text without gaps or duplication. Token limits use the chunk's recorded tokenizer rather than a character-ratio estimate.

## Command reference

```bash
# Previous session/history listing
scripts/finspect runs --limit 20
scripts/finspect invocations --run fr_<uuid> --limit 20
scripts/finspect search-responses --run fr_<uuid> --limit 20

# Replay by the sole required identity
scripts/finspect replay-search <search-response-uuid>

# Stable candidate selection and acquisition
scripts/finspect scrape-candidates <candidate-uuid> [<candidate-uuid> ...] \
  --format markdown --idempotency-key <key>

# Retry only failed items from a prior candidate acquisition.
# A new key creates explicit parent-invocation and retry-parent-attempt lineage.
scripts/finspect retry-candidates <direct-scrape-invocation-uuid> \
  --idempotency-key <new-key>

# Extraction provenance and committed/reused corpus identities
scripts/finspect attempts --candidate <candidate-uuid>
scripts/finspect attempts --run fr_<uuid>
scripts/finspect inspect <candidate|response|attempt|source|snapshot|document|derivation|chunk-uuid>

# Bounded source reading and discovery
scripts/finspect passages <asset-uuid> \
  --limit 20 --max-chars 20000 --max-tokens 4000
scripts/finspect lexical-search "search terms" --run fr_<uuid> \
  --limit 20 --max-chars 20000 --max-tokens 4000
scripts/finspect pattern-search "literal.identifier" --mode literal --run fr_<uuid>
scripts/finspect pattern-search "foo-[0-9]+|bar" --mode regex --run fr_<uuid>
```

Pass `--cursor <next_cursor>` to continue a list, passage, lexical-search, or pattern-search result.

### Replay, repeat, and retry

These are distinct operations:

- `replay-search <response-id>` is read-only. It returns the retained provider response and stable candidate IDs without invoking Firecrawl.
- Repeating `scrape-candidates` with the same idempotency key replays the committed terminal direct-scrape result and performs no duplicate provider call or persistence.
- `retry-candidates <prior-invocation-id> --idempotency-key <new-key>` retries only failed items, preserves parent invocation and attempt lineage, and is itself idempotent when repeated with the same new key.

A new unrelated scrape key is not represented as a retry.

## Migration from file-oriented inspection

| Former filesystem inspection use case | Database-native equivalent |
|---|---|
| list retained acquisition history | `finspect runs`, then `invocations` and `search-responses` |
| directory index / result-rank lookup | `search-responses`, then candidate IDs from `replay-search` |
| read a result file | `inspect <asset-id>` plus `passages <asset-id>` |
| `--skip` / `--lines` | Continue `passages` with its lossless opaque cursor |
| case-insensitive literal grep | `pattern-search <text> --mode literal` |
| case-insensitive regular-expression grep | `pattern-search <regex> --mode regex` |
| word-oriented relevance discovery | `lexical-search`, optionally restricted to a run |
| retry/reuse a prior search | `replay-search <search-response-id>` for read-only reuse; run a new authoritative search only when new provider acquisition is intended |
| scrape selected ranks | `scrape-candidates <candidate-id>...` |
| retry failed selected scrapes | `retry-candidates <prior-invocation-id> --idempotency-key <new-key>` |

`lexical-search` is PostgreSQL full-text search using `plainto_tsquery('simple', ...)`. It is not a regex or arbitrary substring engine. Use `pattern-search` when preserving former grep semantics matters. Regex search is case-insensitive, bounded, and executed with a fixed PostgreSQL statement timeout.

## Failure and process behavior

Missing identifiers, corrupt or missing retained blobs, payloads above the requested replay bound, mixed-run candidate batches, invalid or cross-scope cursors, unknown stored tokenizers, invalid regular expressions, and invalid bounds fail closed.

Successful inspection commands write JSON to stdout and return exit code `0`. Argument failures write a versioned error to stderr and return `2`; typed inspection/not-found/integrity failures return `3`; unexpected persistence failures return `4`. Candidate acquisition and retry return `0` only for `status=complete`; `partial` and `failed` authoritative results are still written to stdout but return exit code `5` so shell and agent callers cannot mistake item failure for command success.

A failed authoritative preflight cannot construct or invoke Firecrawl or another network transport.
