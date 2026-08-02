# Database-native history, replay, and inspection

`finspect` replaces operational inspection of acquisition directories with bounded reads from PostgreSQL and immutable payload reads from `BLOB_ROOT`.

## Authority boundary

- PostgreSQL is authoritative for runs, invocations, search responses, candidates, extraction attempts, provenance, corpus identities, and index jobs.
- `BLOB_ROOT` retains immutable provider payload bytes. `replay-search` verifies the stored SHA-256 and byte length before returning a payload.
- Qdrant remains a rebuildable projection and is not consulted by these commands.
- Valkey is not required for history, replay, inspection, or correctness.
- Candidate acquisition accepts stable candidate UUIDs only. It delegates to the authoritative direct-scrape service, whose database/run/blob/privilege preflight completes before a Firecrawl adapter is constructed or invoked.

All commands emit versioned JSON. Lists use opaque keyset cursors and hard page limits. Passages and lexical matches enforce record, character, and token bounds.

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

# Extraction provenance and committed corpus identities
scripts/finspect attempts --candidate <candidate-uuid>
scripts/finspect attempts --run fr_<uuid>
scripts/finspect inspect <candidate|response|attempt|source|snapshot|document|chunk-uuid>

# Bounded source reading and lexical discovery
scripts/finspect passages <asset-uuid> --limit 20 --max-chars 20000 --max-tokens 4000
scripts/finspect lexical-search "search terms" --run fr_<uuid> \
  --limit 20 --max-chars 20000 --max-tokens 4000
```

Pass `--cursor <next_cursor>` to continue a list, passage, or lexical-search result. Reusing the same candidate-scrape idempotency key replays the committed authoritative result; a new explicit key creates a distinct acquisition attempt.

## Migration from file-oriented inspection

| Former use case | Database-native equivalent |
|---|---|
| session history | `finspect runs`, then `invocations` and `search-responses` |
| directory index / result-rank lookup | `search-responses` plus stable candidate IDs returned by `replay-search` |
| read a result file | `inspect <asset-id>` and `passages <asset-id>` |
| skip/line slice | continue `passages` with its opaque cursor; bound by records, characters, and tokens rather than file lines |
| grep a result directory | `lexical-search`, optionally restricted to a run |
| retry/reuse a prior search | `replay-search <search-response-id>`; no query, path, rank, or directory is required |
| scrape selected ranks | `scrape-candidates <candidate-id>...` |

Explicit user-requested exports remain separate presentation artifacts. They are generated from authoritative records and are never accepted as runtime replay or selection state.

## Failure behavior

Missing identifiers, corrupt/missing retained blobs, payloads larger than the requested replay bound, mixed-run candidate batches, invalid cursors, and invalid bounds fail closed. A failed authoritative preflight cannot invoke Firecrawl or another network transport.
