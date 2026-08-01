# Research Asset Store Architecture

## Authority boundaries

| Component | Role | Recovery rule |
| --- | --- | --- |
| PostgreSQL | Sole authority for sources, snapshots, derivations, chunks, research runs, invocations, transitions, events, budgets, coverage, claims, evidence, semantic provenance, embedding manifests, and jobs | Restore first. Never infer corpus or workflow truth from Qdrant, Valkey, or scratch files. |
| Blob root | Immutable content-addressed payload bytes referenced by PostgreSQL snapshots | Restore with PostgreSQL at one recovery boundary. Report unreferenced hashes before bounded cleanup. |
| Qdrant | Rebuildable dense-retrieval projection | Rebuild a fingerprinted physical collection, reconcile it, then switch the active alias. |
| Valkey | Best-effort worker wakeups and bounded transient cache | Lose or clear it safely. Workers recover PostgreSQL jobs by polling. |
| Scratch files | Disposable acquisition diagnostics and human-readable local output | Delete freely. `_corpus.json` reports committed PostgreSQL identities but is never authority. |

## Data flow

```text
Firecrawl result
  -> PostgreSQL invocation batch transaction
     -> source -> immutable snapshot -> versioned document -> blocks -> chunks
     -> research run and asset links
     -> embedding manifests and index jobs
     -> ordered asset successes and failures
  -> content-addressed blob bytes
  -> optional scratch diagnostics and _corpus.json identity report

Lease-safe worker
  -> claim PostgreSQL jobs with a bounded lease
  -> embed the exact chunk for the job's index definition
  -> idempotently upsert the definition's physical Qdrant collection
  -> complete the exact manifest using its lease token

Agent
  -> corpus-overview/search-assets (compact manifests)
  -> inspect-asset/fetch-passages (bounded expansion)
  -> PostgreSQL retrieval and selection events
```

Corpus rows, batch provenance, manifests, and indexing jobs commit together. Blob writes that precede a rolled-back transaction are reportable orphans, not corpus records. Per-asset savepoints retain successful siblings while recording individual failures; enabled persistence remains fail-closed.

Workflow state follows the same authority boundary. `ResearchRunService` owns atomic compare-and-swap transitions and immutable event/transition ledgers. `WorkflowOperationService` is the wrapper boundary: it validates the run before network work, opens an authoritative invocation, advances only through permitted states, records completion or failure, and gates terminal completion on indexed PostgreSQL assets. `InvocationService` records top-level and child operations directly in PostgreSQL.

## Identity and derivation versioning

- Canonical URL defines a logical source. Serialize ingestion at the source row so concurrent identical content reuses one snapshot.
- Content hash defines immutable snapshot bytes. Link changed content to the prior snapshot.
- Permit multiple normalized documents for one snapshot. Identify each derivation by parser version, normalization version, and normalized-document hash.
- Identify chunks by selected document and chunker version. Retrieval selects only configured active versions.
- Rebuild derivations from authoritative blob bytes with `rederive`; do not create a false snapshot for a parser or chunker upgrade.
- Link every top-level `fc_<uuid>` to a PostgreSQL invocation and every explicit `fr_<uuid>` to one PostgreSQL research run.

## Versioned dense indexes

Bind every index definition to an immutable fingerprint of embedding model, revision, dimension, distance metric, normalization behavior, and instruction-template hash. Name physical collections `research_chunks_<12-character-fingerprint>` and keep `research_chunks_active` as the stable retrieval alias. Never embed a query against an alias backed by another fingerprint: fall back to active-derivation lexical search and expose the mismatch through `doctor`.

Each embedding manifest belongs to one chunk and one index definition. Jobs reference that exact pair. Build replacements without changing live retrieval. Requeue missing points when a physical collection is deleted or damaged even if old jobs were complete. Activate only after manifests, point coverage, schema, and probe-query checks pass. Preserve old collections for explicit rollback; prune only after reviewed dry run and an exact forced target.

## Lease and failure semantics

Claim pending, retryable failed, and expired-running jobs with `FOR UPDATE SKIP LOCKED`. Record lease token, owner, expiration, attempt count, and timestamps. Require the current token to renew or finish a job so stale workers cannot overwrite reclaimed attempts. Move an expired final attempt to dead state and fail its manifest. Qdrant upserts remain idempotent when a worker crashes after projection but before PostgreSQL completion.

Use Valkey only to shorten latency. Push notifications after commit and alternate finite blocking waits with PostgreSQL polling. A lost notification must never strand work. `doctor` reports worker heartbeat, pending age, stale leases, dead jobs, failed batches, active fingerprint, alias target, projection coverage, and endpoint health without mutating state.
