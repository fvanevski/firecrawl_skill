# Research Asset Store Architecture

## Authority boundaries

| Component | Role | Recovery rule |
| --- | --- | --- |
| PostgreSQL | Authoritative sources, snapshots, derivations, chunks, runs, batches, retrieval events, embedding manifests, and jobs | Restore first; never infer corpus truth from Qdrant or Valkey. |
| Blob root | Immutable, content-addressed raw payload bytes referenced by snapshots | Restore with PostgreSQL at one recovery boundary; report unreferenced hashes before bounded cleanup. |
| Qdrant | Rebuildable dense-retrieval projection | Rebuild a versioned physical collection, verify it, then switch the active alias. |
| Valkey | Best-effort worker wakeups and transient cache | Lose or clear it safely; the worker recovers PostgreSQL jobs by polling. |
| Scratch and Catalog v5 | Compatibility, diagnostics, and acquisition audit | Reconstruct `_corpus.json` from a committed invocation batch when needed. |

## Data flow

```text
Firecrawl result
  -> invocation batch transaction
     -> source -> immutable snapshot -> versioned document -> blocks -> chunks
     -> research run and asset links
     -> embedding manifests and index jobs
     -> ordered asset successes and failures
  -> content-addressed blob bytes
  -> _corpus.json compatibility export

Lease-safe worker
  -> claim PostgreSQL jobs with a bounded lease
  -> embed the exact chunk for the job's index definition
  -> idempotently upsert the definition's physical Qdrant collection
  -> complete the exact manifest using its lease token

Agent
  -> corpus-overview/search-assets (compact manifests)
  -> inspect-asset/fetch-passages (bounded expansion)
  -> linked retrieval and selection events
```

Commit corpus rows, batch provenance, manifests, and indexing jobs together. Treat blob writes that precede a rolled-back transaction as reportable orphans, not as corpus records. Use per-asset savepoints so one failed result does not erase retained siblings; record the failure and return nonzero because enabled persistence is fail-closed.

## Identity and derivation versioning

- Canonical URL defines a logical source. Serialize ingestion at the source row so concurrent identical content reuses one snapshot instead of surfacing a uniqueness error.
- Content hash defines immutable snapshot bytes. Link changed content to the prior snapshot.
- Permit multiple normalized documents for one snapshot. Identify each derivation by parser version, normalization version, and normalized-document hash.
- Identify chunks by the selected document and chunker version. Retrieval selects only the configured active parser, normalizer, and chunker versions.
- Rebuild derivations from authoritative blob bytes with `rederive`; do not create a false snapshot for a parser or chunker upgrade.
- Link every top-level `fc_<uuid>` to an ingestion batch and every explicit `fr_<uuid>` to one database research run. Keep ordered per-asset IDs and retrieval events so a delivered claim can be traced to exact source, snapshot, document, and chunk IDs.

## Versioned dense indexes

Bind every index definition to an immutable fingerprint of embedding model, revision, dimension, distance metric, normalization behavior, and instruction-template hash. Name physical collections `research_chunks_<12-character-fingerprint>` and keep `research_chunks_active` as the stable retrieval alias. Never embed a query against an alias backed by another fingerprint: fall back to active-derivation lexical search and surface the mismatch through health checks.

Key each embedding manifest by chunk and index definition. Enforce the job's `(manifest_id, index_definition_id)` as a composite foreign key to that exact manifest; never update manifests by chunk or collection alone. Build replacements without changing live retrieval. Requeue missing points when a physical collection was deleted or damaged even if its old jobs were complete. Activate only after all active-derivation manifests are complete, point coverage and collection schema reconcile, and a probe query succeeds. Journal prepared, switched, and finalized activation states so interrupted cutovers can be reconciled. Preserve old collections for explicit rollback; prune only by reviewed dry run plus an exact index ID and `--force`.

## Lease and failure semantics

Claim pending, retryable failed, and expired-running jobs with `FOR UPDATE SKIP LOCKED`, one immediately before each job begins so unprocessed batch members do not age under a shared lease start. Record a lease token, owner, expiration, attempt count, and timestamps. Require the current token to renew or finish a job so a stale worker cannot overwrite a reclaimed attempt. Move an expired final attempt directly to dead state and fail its manifest. Qdrant upserts remain idempotent when a worker crashes after projection but before completion.

Use Valkey only to shorten latency. Push notifications after commit; alternate finite blocking waits with PostgreSQL draining. A lost notification must never strand work. Move attempts beyond the configured maximum to dead-letter state and expose them, stale leases, oldest pending age, worker heartbeat, active fingerprint, alias target, and projection coverage through the read-only `doctor` report.

Make default `doctor` strictly read-only. Do not create directories, probe files, collections, aliases, migrations, or cache entries. Put initialization, activation, repair, reconciliation, and cleanup behind explicit commands.
