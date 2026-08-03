# Research Asset Store Architecture

## Target A authority boundaries

| Component | Current role | Recovery rule |
|---|---|---|
| PostgreSQL | Authoritative runs, invocations, transitions, events, search responses, stable candidates, extraction attempts, provenance, corpus identities, evidence, audits, and durable jobs | Restore first; never infer these records from another layer |
| `BLOB_ROOT` | Immutable content-addressed provider payload bytes referenced by PostgreSQL snapshots | Restore at the same logical boundary as PostgreSQL and verify digests |
| Qdrant | Rebuildable dense-retrieval projection | Build a compatible fingerprinted collection from PostgreSQL chunks, reconcile, then switch the active alias |
| Valkey | Optional worker wakeups and bounded transient coordination | Recreate safely; workers recover from PostgreSQL polling |
| Ephemeral files | Bounded process-local implementation details | Delete freely; never read them as workflow, replay, history, selection, or corpus state |

“PostgreSQL-authoritative” describes the workflow, identity, metadata, provenance, and job boundary. It does not mean provider payload bytes are stored in PostgreSQL under Target A. `BLOB_ROOT` remains the byte store.

## Data flow

```text
Firecrawl provider response
  -> authoritative preflight already completed
  -> PostgreSQL invocation and acquisition transaction
     -> search response and stable candidates, or direct extraction attempt
     -> source -> immutable snapshot -> versioned document -> blocks -> chunks
     -> run links, provenance, embedding manifests, and index jobs
  -> immutable payload bytes written under BLOB_ROOT by digest
  -> bounded stable-ID result returned to the caller

Lease-safe worker
  -> claims PostgreSQL jobs with bounded leases
  -> embeds the exact chunk for one immutable index definition
  -> idempotently upserts the matching physical Qdrant collection
  -> completes the manifest with the current lease token

Inspection
  -> PostgreSQL lists and stable identities
  -> verified bounded reads from BLOB_ROOT for retained provider payloads
  -> PostgreSQL lexical retrieval plus compatible Qdrant projection
```

No successful acquisition path is mediated by a local manifest or staging directory. A failed authoritative preflight occurs before Firecrawl construction or network execution.

## Identity and derivation versioning

- Canonical URL identifies a logical source.
- Content digest identifies immutable snapshot bytes.
- Multiple versioned document derivations may reference one snapshot.
- Parser, normalizer, chunker, tokenizer, and document hashes identify derivation behavior.
- Stable candidate IDs—not ranks—select retained search candidates.
- Stable response IDs replay retained provider responses.
- Every top-level operation records an `fc_<uuid>` invocation attached to an `fr_<uuid>` run.

Rederive parser or chunker output from retained blob bytes. Do not create a false new snapshot for a derivation upgrade.

## Transaction and failure semantics

- Preflight validates schema head, writable privileges, durable blob storage, and run eligibility.
- Search response and candidate rows commit before search success is reported.
- Direct-scrape batches retain item-level success and failure with ordered authoritative identities.
- Per-item savepoints may retain successful siblings, but a failed item remains explicit and causes a nonzero partial result.
- Idempotency keys replay identical committed input and reject conflicting reuse.
- No network or Firecrawl invocation occurs after failed preflight.
- A blob written before a rolled-back metadata transaction is an orphan, not a corpus record; report it for bounded cleanup.

## Versioned Qdrant indexes

An index definition fingerprints model, revision, vector dimension, distance metric, normalization behavior, and instruction template. Physical collections use `research_chunks_<fingerprint>` and retrieval uses `research_chunks_active`.

Build replacements without modifying authoritative corpus data. Activate only after manifest completeness, point reconciliation, schema compatibility, and probe success. On alias or fingerprint mismatch, skip dense query embedding, remain lexical, and report the mismatch through `doctor`.

## Lease and Valkey semantics

Workers claim jobs with `FOR UPDATE SKIP LOCKED`, lease token, owner, expiration, and attempt count. Stale workers cannot complete reclaimed work. Qdrant upserts are idempotent when a process dies after projection but before PostgreSQL completion.

Valkey notifications occur after commit and may be lost. Finite waits alternate with PostgreSQL polling, so notification loss cannot strand jobs.

## Future PostgreSQL payload migration

A future target may move immutable payload bytes into PostgreSQL or a PostgreSQL-managed large-object design. That is outside Target A and must separately define:

- schema and digest invariants;
- transactional byte/metadata semantics;
- database growth and vacuum behavior;
- backup, restore, replication, and retention;
- online migration and rollback;
- replacement or retirement of `BLOB_ROOT`.

That future target is not implemented. Current documentation, tests, and recovery procedures must not be interpreted as implementing it.
