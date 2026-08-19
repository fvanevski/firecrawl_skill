# PostgreSQL Migration Guide

## 1. Migration principles

PostgreSQL is authoritative for workflow, acquisition provenance, corpus identities, staged asset promotion, exact completion membership, and durable jobs. Under Target A, immutable provider payload bytes remain in `BLOB_ROOT`; Qdrant is rebuildable and Valkey is transient.

Alembic migrations are forward-only and transactional. The current clean schema head is `0040_asset_promotion_membership`.

A future migration that stores payload bytes in PostgreSQL is not part of this release. Do not delete or bypass `BLOB_ROOT` based on the phrase “PostgreSQL-authoritative.”

## 2. Pre-migration checklist

```bash
git status --short
scripts/research-db status
scripts/research-db verify-blobs
scripts/research-db doctor
```

Before changing code or schema:

1. stop acquisition writers and the index worker;
2. back up PostgreSQL and `BLOB_ROOT` at one logical boundary;
3. record active Qdrant index and alias state;
4. identify whether the source is:
   - a current supported PostgreSQL deployment;
   - a legacy acquisition tree that has not been imported; or
   - an unsupported database from an older migration lineage.

Do not manually stamp Alembic or infer authoritative state from local acquisition files, Qdrant points, or historical run-asset rows.

## 3. Running migrations

For a supported current-lineage database:

```bash
scripts/research-db migrate
scripts/research-db status
scripts/research-db ingest-ready
scripts/research-db doctor
```

Expected schema state:

```json
{
  "current": "0040_asset_promotion_membership",
  "head": "0040_asset_promotion_membership",
  "at_head": true
}
```

`ingest-ready` and `doctor` must pass after migration.

## 4. Migration sequence

| Revision | Purpose |
|---|---|
| `0001_research_store` | Sources, snapshots, documents, chunks, manifests, jobs, and retrieval events |
| `0002_research_store_integrity` | Content-addressed integrity, index definitions, batches, run assets, leases, and index lifecycle |
| `0003_job_manifest_integrity`, `0004_manifest_definition_key` | Job and manifest constraints |
| `0006_workflow_state` | Authoritative runs, invocations, events, semantic records, and transition ledger |
| `0007_budget_snapshots` | Immutable run resource authorization |
| `0009_search_plans`–`0016_invocation_events` | Search planning, retained responses, stable candidates, coverage, decisions, and invocation events |
| `0017_claims_evidence`–`0030_duplicate_groups` | Claims, evidence, audits, extraction provenance, derivations, retrieval traces, packets, and duplicate groups |
| `0031_synthesis_stages`, `0032_semantic_cache`, `0033_resource_governance`, `0034_add_validation_stage` | Synthesis, cache, resource governance, and validation |
| `0035_index_point_counts`–`0038_postgres_authority` | Index verification, measured telemetry, and consolidated PostgreSQL authority |
| `0039_index_checkpoint_guard` | Exact indexing checkpoint membership, lifecycle CAS, bounded recovery, and terminal transition guards |
| `0040_asset_promotion_membership` | Explicit asset-promotion stages, append-only provenance, PostgreSQL-verified membership seals, and checkpoint binding |

Removed revisions and runtime compatibility tables are not migration targets.

### 4.1 Revision 0040 compatibility boundary

Revision 0040 is additive and does not fabricate promotion history for pre-existing `research_run_assets`. Rows present before the migration remain readable as:

```text
current_stage = unknown
provenance = legacy_unstructured
```

No discovery, extraction, retention-policy, evidence-eligibility, or completion-critical event is backfilled. Existing active checkpoints created before revision 0040 remain readable under the revision-0039 compatibility contract. A new checkpoint requires an exact active asset-membership seal and fails closed if a run still contains unknown historical assets.

Any evidence-bearing repair must be implemented as a new forward migration or explicit operator workflow. Do not update promotion tables manually merely to make a completion gate pass. See `asset-promotion-membership.md` for the stage, sealing, reopen/reseal, and historical-read contracts.

## 5. Legacy acquisition-tree compatibility boundary

RC-9 does not contain a runtime importer.

The exact last main revision that still provides `research-db import-scratch` is:

```text
82d3369c0be9bba381f38b598c3b05ed4b683ae6
```

That revision immediately precedes RC-6 merge commit:

```text
1aaa92f7c3a84ea1ed210947130b120cc814826e
```

No tag name is asserted. Use the exact commit to avoid ambiguity.

### 5.1 Import before upgrading

Create an isolated worktree at the compatibility revision and point it at a clean current-schema PostgreSQL database and durable blob root:

```bash
git fetch origin
git worktree add ../firecrawl-pre-rc6 82d3369c0be9bba381f38b598c3b05ed4b683ae6
cd ../firecrawl-pre-rc6
source scripts/research-env

scripts/research-db status
scripts/research-db ingest-ready

scripts/research-db import-scratch '<legacy-tree>' \
  --dry-run \
  --report import-dry-run.json

scripts/research-db import-scratch '<legacy-tree>' \
  --report import-result.json

scripts/research-db verify-blobs
scripts/research-db doctor
```

Review item failures and stable imported identities before removing the legacy source tree. Then return to the current checkout, run `migrate`, `status`, `ingest-ready`, and `doctor`.

A database created by a materially older unsupported schema lineage must not be stamped forward. Use a clean supported database for the compatibility import or an external one-shot migration tool.

### 5.2 Current equivalents

| Removed interface | Current authoritative equivalent |
|---|---|
| `scripts/fread --history` | `scripts/finspect runs`, `invocations`, and `search-responses` |
| file or rank replay | `scripts/finspect replay-search <search-response-id>` |
| `--scrape-ranks` | `scripts/finspect scrape-candidates <candidate-id>...` |
| `--reuse-search` | read-only `replay-search`; new provider acquisition only when intended |
| `--dir` | no runtime destination; use `export-invocation` or `export-run` for explicit presentation output |
| `--output-dir` | no runtime destination; use explicit database-native export |
| `_corpus.json` ingestion or completion | direct PostgreSQL service records and stable IDs |
| `FIRECRAWL_RESEARCH_PERSIST=auto|on|off` | unconditional authoritative persistence for supported acquisition |

## 6. Interrupted migration repair

After interruption:

```bash
scripts/research-db status
scripts/research-db migrate
scripts/research-db status
scripts/research-db ingest-ready
scripts/research-db doctor
```

If Alembic remains at the prior revision, rerun the migration. If revision metadata and required objects disagree, restore or reset; do not edit schema state manually.

For an interrupted `0039 → 0040` upgrade, verify that Alembic either remains at `0039_index_checkpoint_guard` with no committed revision-0040 objects or reaches `0040_asset_promotion_membership` with all promotion, event, seal, member, and checkpoint-binding objects present. PostgreSQL transactional DDL prevents a supported migration from committing a half-applied revision.

## 7. Forward-repair migrations

Future corrections must be new revisions that preserve:

- PostgreSQL workflow, asset-promotion, and identity authority;
- `BLOB_ROOT` payload integrity under Target A;
- append-only transition, event, and promotion ledgers;
- database-verified exact membership hashes and counts;
- idempotent retries and lifecycle compare-and-swap behavior;
- explicit recovery and compatibility notes;
- fresh-database and populated-prior-head tests when in-place upgrade is supported.

A future PostgreSQL-payload migration must be named and tested as a new target, not smuggled into a documentation or refactor change.

## 8. Rollback boundary

Before upgrading, retain a matching PostgreSQL dump and `BLOB_ROOT` backup. If rollback is required:

1. stop writers and workers;
2. restore PostgreSQL and `BLOB_ROOT` from the same pre-upgrade boundary;
3. deploy the exact code revision compatible with that boundary;
4. rebuild or reactivate Qdrant;
5. recreate Valkey;
6. run `verify-blobs`, `status`, `ingest-ready`, and `doctor`.

Revision 0040 does not implement a destructive downgrade. Its `downgrade()` raises a forward-only error. Restoring only PostgreSQL, or only `BLOB_ROOT`, is not a valid rollback because extraction provenance and immutable payload references must remain at the same logical boundary.

Do not attempt to reconstruct authoritative records from presentation exports, Qdrant, Valkey, or the legacy source tree. The pre-RC-6 revision should be deployed only for bounded import or rollback work, not retained as the normal runtime.

## 9. Migration testing

```bash
export RESEARCH_STORE_TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1/firecrawl_test'
export RESEARCH_STORE_TEST_ALLOW_RESET='firecrawl_test'

env PYTHONDONTWRITEBYTECODE=1 \
  pytest -q -p no:cacheprovider \
  tests/contract/test_asset_promotion_contract.py \
  tests/unit/test_asset_promotion_integration.py \
  tests/unit/test_asset_promotion_reopen_concurrency.py \
  tests/unit/test_asset_promotion_migration_compat.py \
  tests/integration/test_research_store_integration.py \
  tests/unit/test_workflow_service.py \
  tests/contract/test_documentation.py
```

Release acceptance also requires Python 3.11 and 3.12, Ruff, disposable Qdrant and Valkey, worker recovery, exact-head evidence, and the authoritative wrapper gates.
