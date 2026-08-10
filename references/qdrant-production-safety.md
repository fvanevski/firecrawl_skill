# Qdrant production-safety boundary

PostgreSQL is authoritative for index definitions, lifecycle state, manifests,
jobs, and activation history. Qdrant is a rebuildable projection. The stable
configured alias (normally `research_chunks_active`) is nevertheless an
operational production boundary: routine workers, tests, and diagnostics must
not silently delete its target or repoint it.

## Required alias invariant

A healthy active projection requires all of the following at the same instant:

1. PostgreSQL has exactly one `index_definitions` row with
   `lifecycle_status='active'`.
2. The configured `QDRANT_ALIAS` exists.
3. That exact alias targets the active PostgreSQL definition's
   `physical_collection`.
4. The collection schema matches the active definition's vector dimension and
   distance metric.
5. For the configured runtime, the active definition also matches the current
   embedding fingerprint and expected physical collection.

An unrelated alias that happens to target the same collection does **not**
satisfy this contract. Missing aliases, wrong targets, or multiple active
PostgreSQL definitions are cross-store activation drift and must fail closed.

The shared implementation for this comparison lives in
`scripts/research_store/qdrant_authority.py` so doctor, reconciliation, and
release-safety probes do not maintain divergent alias semantics.

## Worker invariant

Routine `IndexWorker` processing is non-destructive. Workers use
`QdrantIndex.require_compatible_schema()` and therefore:

- fail if the target collection is missing;
- fail if its vector schema is incompatible;
- never create, delete, or recreate a collection; and
- never mutate aliases.

Collection creation/rebuild remains an explicit `index-build` operation.
`ensure_schema()` may rebuild an incompatible **unaliased** projection, but it
refuses to delete any collection currently targeted by an alias. Alias cutover
is performed only by the supported activation/rollback lifecycle.

## Activation and rollback

Use the supported lifecycle rather than direct Qdrant mutation:

```bash
scripts/research-db index-build --current-config --all
python3 scripts/drain_index_jobs.py --batch-size 64
scripts/research-db index-reconcile
scripts/research-db index-activate '<index-definition-id>'
```

Rollback uses the same verified lifecycle:

```bash
scripts/research-db index-rollback '<prior-index-definition-id>'
```

Activation requires complete PostgreSQL coverage and compatible Qdrant points
before the alias switch. The activation journal records the prepared, switched,
and completed transition so recovery can reconcile an interrupted cutover.

Do not add a blanket guard to `QdrantIndex.switch_alias()` that rejects the
configured production alias. That would also disable the supported activation
and rollback mechanism. Safety belongs at the caller/lifecycle boundary.

## Release preflight durability

A release worker probe must prove more than successful indexing of its transient
point. Before the worker probe, capture the exact required alias target,
PostgreSQL-active definition, vector schema, point count, and a bounded sample
of existing point IDs. After probe cleanup, verify that:

- alias and active-definition identity are unchanged;
- schema identity is unchanged;
- total point count returned exactly to the captured baseline; and
- every sampled baseline point still exists.

Exact point-count restoration is required: both lost baseline points and leaked
transient probe points fail the preflight. This is intentionally fail-closed. A
worker probe that succeeds while changing pre-existing projection state or
leaving probe state behind is a release-gate failure.

## Test isolation contract

`RESEARCH_STORE_TEST_DATABASE_URL` authorizes only PostgreSQL test mutation. It
does **not** authorize Qdrant mutation.

Any test that creates, deletes, rebuilds, or repoints Qdrant resources must use
an explicitly disposable endpoint:

```bash
export RESEARCH_STORE_TEST_QDRANT_URL='http://127.0.0.1:6334'
export RESEARCH_STORE_TEST_QDRANT_ALLOW_RESET="$RESEARCH_STORE_TEST_QDRANT_URL"
```

The two values must match exactly. This is analogous to the PostgreSQL
`RESEARCH_STORE_TEST_ALLOW_RESET` contract and prevents an inherited host
`QDRANT_URL` from being treated as disposable state.

The pytest support in `scripts/qdrant_test_support.py` redirects Qdrant-aware
integration tests only after this authorization is present. Cleanup is bounded
to the configured test alias and explicitly owned physical collections. It must
never enumerate a shared Qdrant instance and delete every collection except one
that looks like production.

CI jobs satisfy this contract only for Qdrant containers they start themselves.
For local validation, use a separate disposable Qdrant container/port rather
than the live production service.

## Recovery after projection loss or alias drift

If PostgreSQL still contains complete manifests/jobs but the active Qdrant
projection is missing or empty, do not manufacture alias state or mark jobs
complete. Treat PostgreSQL as authority and rebuild:

```bash
scripts/research-db index-list
scripts/research-db index-reconcile
scripts/research-db index-build --current-config --all
python3 scripts/drain_index_jobs.py --batch-size 64
scripts/research-db index-reconcile
scripts/research-db index-activate '<verified-index-definition-id>'
scripts/research-db doctor
```

A clean recovery requires exact alias/definition agreement, compatible schema,
zero missing/orphaned active points, and healthy doctor/reconciliation output.
Only then may release preflight or an ARC-17 Real campaign continue.
