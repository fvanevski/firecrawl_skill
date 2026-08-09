# PostgreSQL-to-Qdrant reconciliation

Issue #222 defines reconciliation as an **authority check**, not a Qdrant
inventory dump. PostgreSQL owns run membership, manifests, job state, index
definitions, and lifecycle history. Qdrant remains a rebuildable projection.

## Command contract

```bash
research-db index-reconcile <run-id-or-external-id>
research-db index-reconcile <run-id-or-external-id> --repair
```

`index-reconcile` is read-only unless `--repair` is present. A non-repair run
must not create payload indexes, requeue jobs, delete points, recreate a
collection, or repoint an alias. It exits `0` only when reconciliation is clean,
`1` when discrepancies remain, and `2` when an authoritative scope cannot be
established (for example, a pre-membership-seal historical checkpoint).

For compatibility, omitting the run identifier produces a projection-wide
health report. That mode is deliberately labeled `scope=projection` and must
not be interpreted as historical run provenance. `reconcile-qdrant` is retained
as a command alias with the same semantics.

## Authoritative run membership

A run-scoped reconciliation is anchored to the latest **completed**
`indexing_checkpoints` row for the run. The checkpoint persists:

- exact `entity_ids`;
- `expected_count` and membership SHA-256;
- immutable index fingerprint; and
- the `asset_membership_seal_id` and membership digest established by the
  asset-promotion contract.

The reconciler independently reloads the bound
`run_asset_membership_seals`/`run_asset_membership_members` rows and requires
the persisted member chunk IDs to equal the checkpoint `entity_ids`. It never
reconstructs a historical run from the current parser/chunker configuration or
from host-wide corpus membership. If the checkpoint predates the sealed
membership contract, reconciliation fails closed rather than inventing
historical provenance.

The checkpoint fingerprint must resolve to exactly one PostgreSQL
`index_definitions` row. That row determines the expected physical collection,
vector dimension, distance metric, and model identity.

## What is checked

The v2 report separates two different membership questions:

1. **Run coverage** — every sealed checkpoint chunk must have its expected
   manifest/job and Qdrant point.
2. **Definition orphans** — every Qdrant point in the physical collection must
   still be backed by a PostgreSQL manifest/chunk for that exact index
   definition. Points belonging to other runs are therefore not falsely called
   run orphans.

The reconciler reports and fails on:

- missing or incomplete manifests for sealed chunks;
- incomplete upsert jobs for sealed chunks;
- missing sealed Qdrant points;
- Qdrant points orphaned from the PostgreSQL index definition;
- vector collection absence/incompatibility;
- active alias mismatch for run-scoped reconciliation;
- payload identity drift in `snapshot_id`, `document_id`, `source_id`, `domain`,
  or `published_at`;
- missing or type-incompatible payload indexes; and
- inactive/unknown shard replicas or in-progress shard transfer/resharding
  topology.

Payload validation is exhaustive over the sealed set. Exact point retrieval is
performed in bounded batches (256 IDs per request); a 1,376-point audited run
therefore requires multiple batches and cannot silently pass because only an
arbitrary sample happened to match.

## Qdrant payload-index contract

Collection details expose payload indexes through `result.payload_schema`.
Issue #222 uses the following field types:

| Field | Qdrant schema | Reason |
|---|---|---|
| `snapshot_id` | `uuid` | exact UUID identity/filtering |
| `document_id` | `uuid` | exact UUID identity/filtering |
| `source_id` | `uuid` | exact UUID identity/filtering |
| `domain` | `keyword` | exact categorical/domain filtering |
| `published_at` | `datetime` | date/range filtering |

`index-build` provisions missing indexes because it is already an explicit
projection-write operation. Read-only reconciliation only inspects them.
`--repair` may create **missing** typed indexes. An incompatible existing index
is reported and is not silently deleted/replaced.

## Shard health

Shard/replica state is read from Qdrant's collection-cluster endpoint
`GET /collections/{collection}/cluster`, using `local_shards`, `remote_shards`,
`shard_transfers`, and `resharding_operations`. No synthetic "active shard 0"
fallback is permitted. Reconciliation fails closed when no shard state is
available, any replica state is not `active`, or topology movement is in
progress.

Shard topology is observational here. `--repair` does not rewrite distributed
Qdrant topology.

## Repair boundary

`--repair` is intentionally bounded by the same PostgreSQL authority used for
inspection. It may:

- create/reuse manifests for exact sealed chunks and requeue their durable
  upsert jobs;
- requeue points whose authoritative payload identity drifted;
- delete Qdrant points that have no PostgreSQL membership in the exact index
  definition; and
- create missing typed payload indexes.

It does **not** silently repair vector-schema mismatches by deleting a
collection, repoint aliases, or alter shard topology. Vector rebuilds and alias
activation must continue through the existing `index-build` / `index-activate`
(or rollback) lifecycle so PostgreSQL activation history remains authoritative.
Because requeued point repair is asynchronous, a repair response includes a
fresh `post_repair` observation and may remain non-clean until the index worker
finishes the durable jobs.

## HNSW and full-scan threshold

Qdrant's `full_scan_threshold` / `full_scan_threshold_kb` is a query-planning
and indexing optimization: below the configured threshold Qdrant may prefer a
full scan rather than HNSW traversal. That behavior is **not** a reconciliation
failure and must not be used as a proxy for index health. Operational tuning of
that threshold belongs to performance configuration; reconciliation checks
membership, schema, payload identity, alias target, and shard health instead.

## Compatibility, migration, and rollback

This correction adds no PostgreSQL schema migration. It consumes the completed
checkpoint and asset-membership columns already introduced by the dependency
chain for #211. Pre-seal historical checkpoints are deliberately reported as
non-authoritative rather than upgraded with inferred data.

Code rollback requires no database downgrade. Qdrant payload indexes created by
an explicit `index-build` or `--repair` are additive projection metadata and may
remain after a code rollback. Orphan deletion and job requeue are derived from
PostgreSQL authority; durable worker replay remains idempotent.

## Required regression matrix

The test suite covers:

- exact complete reconciliation;
- missing and orphaned points;
- a true payload mismatch with exact point membership preserved;
- alias mismatch;
- vector-schema mismatch;
- inactive/empty shard topology (contract-level because a healthy standalone
  disposable Qdrant cannot safely be forced into a dead replica state);
- typed/idempotent payload-index provisioning; and
- the audited `1376 expected / 1376 present` case with an exhaustive payload
  scan spanning at least six 256-ID batches.
