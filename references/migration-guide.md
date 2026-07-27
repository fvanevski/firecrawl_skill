<!-- @format -->

# Migration Guide

Alembic is the sole PostgreSQL schema authority for the Firecrawl Research Skill platform. All migrations are **forward-only** — downgrades are explicitly unsupported. Recovery from a failed migration is via PostgreSQL backup restore or a reviewed forward-repair migration.

## Table of contents

1. [Migration principles](#1-migration-principles)
2. [Pre-migration checklist](#2-pre-migration-checklist)
3. [Running migrations](#3-running-migrations)
4. [Migration catalog](#4-migration-catalog)
5. [Interrupted migration repair](#5-interrupted-migration-repair)
6. [Forward-repair migrations](#6-forward-repair-migrations)
7. [Migration testing](#7-migration-testing)

---

## 1. Migration principles

1. **Forward-only:** Every migration's `downgrade()` raises `RuntimeError`. There are no rollback DDL statements.
2. **Additive:** Migrations add tables, columns, indexes, and constraints. They never drop or modify existing data.
3. **Transactional:** Alembic DDL runs in a single PostgreSQL transaction. An interrupted migration rolls back completely.
4. **Idempotent where possible:** Later migrations use `IF NOT EXISTS` guards and `ON CONFLICT DO NOTHING` for `schema_migrations` inserts.
5. **Phase preservation:** No migration rewrites corpus, snapshot, derivation, index, job, lease, or provenance rows from earlier phases.
6. **Schema version tracking:** Each migration inserts its version into `schema_migrations`. The `alembic_version` table tracks the current head.

## 2. Pre-migration checklist

Before running any migration:

1. **Stop ingestion:** Stop the worker and any active acquisition.
   ```bash
   systemctl --user stop firecrawl-research-indexer.service
   ```
2. **Capture a PostgreSQL backup:**
   ```bash
   pg_dump --format=custom --file="/tmp/firecrawl-pre-migration-$(date +%Y%m%d-%H%M%S).dump" "$DATABASE_URL"
   ```
3. **Capture blob inventory:**
   ```bash
   find "$BLOB_ROOT" -type f -exec sha256sum {} + > "/tmp/blob-inventory-$(date +%Y%m%d-%H%M%S).txt" 2>/dev/null || true
   ```
4. **Record current schema state:**
   ```bash
   rtk proxy "<skill-root>/scripts/research-db" status
   rtk proxy "<skill-root>/scripts/research-db" doctor
   ```
5. **Record Qdrant state:**
   ```bash
   rtk proxy "<skill-root>/scripts/research-db" index-list
   ```
6. **Verify the working tree is clean:**
   ```bash
   git status --short
   ```
7. **Ensure `DATABASE_URL` is set** and points to the target database.

## 3. Running migrations

### 3.1 Apply all pending migrations

```bash
rtk proxy "<skill-root>/scripts/research-db" migrate
```

This runs all pending Alembic revisions up to the current head.

### 3.2 Verify the result

```bash
# Check the current Alembic revision
rtk proxy "<skill-root>/scripts/research-db" status

# Verify the schema is consistent
rtk proxy "<skill-root>/scripts/research-db" doctor

# Verify the store is writable
rtk proxy "<skill-root>/scripts/research-db" ingest-ready
```

### 3.3 Fresh migration (new database)

```bash
# Set DATABASE_URL to a freshly created database
export DATABASE_URL='postgresql://research:...@localhost/research_new'

# Run migrations
rtk proxy "<skill-root>/scripts/research-db" migrate

# Verify
rtk proxy "<skill-root>/scripts/research-db" status
rtk proxy "<skill-root>/scripts/research-db" ingest-ready
```

### 3.4 Upgrade from a specific revision

Alembic applies all pending revisions automatically. To inspect which revisions are pending:

```bash
# Check current vs head
rtk proxy "<skill-root>/scripts/research-db" status
```

If the status output shows the current revision differs from head, pending migrations exist.

## 4. Migration catalog

### Phase 1 — Core corpus and indexing

| Revision | Name | Description | Tables added | Impact |
|----------|------|-------------|--------------|--------|
| `0001` | `research_store` | Initial authoritative research asset store | `research_sources`, `research_snapshots`, `research_documents`, `research_chunks`, `research_manifests`, `research_jobs`, `research_points`, `research_chunk_manifests`, `research_chunks_active`, `research_blob_store` | Foundation: corpus, snapshots, derivations, chunks, indexing jobs, Qdrant points |
| `0002` | `research_store_integrity` | Integrity constraints, indexes, and triggers | Constraints/indexes on all v1 tables | Enforces FK integrity, content-hash uniqueness, chunk ordering |
| `0003` | `job_manifest_integrity` | Composite `(manifest_id, index_definition_id)` job constraint | Constraints on `research_jobs` | Prevents jobs from spanning incompatible manifests |
| `0004` | `drop_legacy_manifest_uniqueness` | Repair stale v1 embedding uniqueness constraint | Removes over-broad uniqueness | Fixes v1 constraint that blocked multiple embeddings per chunk |

### Phase 5 — Workflow state and budget

| Revision | Name | Description | Tables added | Impact |
|----------|------|-------------|--------------|--------|
| `0005` | `run_lifecycle` | Research run lifecycle columns and tables | `research_runs` (new columns), `research_run_lifecycle` | Basic run state tracking |
| `0006` | `workflow_state` | Authoritative workflow-state foundation | `research_invocations`, `research_events`, `research_specs`, `semantic_calls`, `semantic_artifacts`, `compatibility_exports`, `research_run_transitions` | Full workflow authority: invocations, events, specs, semantic provenance, transitions |
| `0007` | `budget_snapshots` | Immutable, versioned budget snapshots | `research_budget_snapshots` | Budget policy enforcement with deterministic caps |
| `0008` | `legacy_adapter_comparisons` | Append-only legacy adapter comparison records | `legacy_adapter_comparisons` | Shadow-mode comparison between legacy and new workflow paths |

### Phase 6 — Search, candidates, coverage, extraction

| Revision | Name | Description | Tables added | Impact |
|----------|------|-------------|--------------|--------|
| `0009` | `search_plans` | Search plan and query tracking | `search_plans`, `search_plan_queries` | Structured search planning with query-level metadata |
| `0010` | `search_responses` | Raw search response manifests | `search_responses` | Immutable raw search responses for replay |
| `0011` | `candidate_identity` | First-class candidate records | `search_candidates`, `candidate_occurrences`, `candidate_groups` | Candidates as first-class assets, not just scrape results |
| `0012` | `coverage_events` | Coverage ledger foundation | `coverage_events`, `coverage_snapshots` | Coverage-led research control |
| `0013` | `strategy_revisions` | Strategy revision proposals | `strategy_revisions` | Adaptive search revision with LLM proposals |
| `0014` | `coverage_event_types` | Expanded coverage event types | Constraints on `coverage_events` | Additional event types for finer coverage tracking |
| `0015` | `terminal_decisions` | Terminal state enforcement | Constraints on `research_runs` | Runs in terminal states reject new acquisition |
| `0016` | `invocation_events` | Expanded invocation and event tracking | Constraints on `research_invocations`, `research_events` | More detailed operation tracking |
| `0017` | `claims_evidence` | Claim-to-evidence bindings | `research_claims`, `claim_evidence_links` | Structured claim binding to evidence passages |
| `0018` | `audit_assessments` | Staged LLM audit assessments | `audit_assessments` | Multi-stage audit with rubric, acquisition, evidence, synthesis |
| `0019` | `audit_identity` | Audit identity and provenance | Constraints on `audit_assessments` | Audit identity enforcement |
| `0020` | `catalog_import_tracking` | Catalog v5 import tracking | `catalog_import_records` | Track migration from scratch to PostgreSQL |
| `0021` | `extraction_attempts` | Extraction attempt records | `extraction_attempts` | Track individual scrape/extraction attempts |
| `0022` | `extraction_attempt_linkage` | Link extraction to candidates | FK on `extraction_attempts` | Connect extraction attempts to candidate records |
| `0023` | `parser_version` | Document parser version tracking | Column on documents | Track which parser version produced each document |

### Phase 6 continued — Derivation, retrieval, evidence

| Revision | Name | Description | Tables added | Impact |
|----------|------|-------------|--------------|--------|
| `0024` | `normalized_blocks` | Normalized blocks and transformations | `research_normalized_blocks`, `research_transformations` | Hierarchical document normalization |
| `0025` | `hierarchical_chunks` | Hierarchical chunk structure | `research_hierarchical_chunks` | Chunk hierarchy for better retrieval |
| `0026` | `document_derivations` | Document derivation management | `research_document_derivations` | Versioned document derivations with activation |
| `0027` | `retrieval_executions` | Retrieval execution tracking | `retrieval_executions` | Track retrieval query executions |
| `0028` | `retrieval_trace` | Retrieval trace records | `retrieval_traces` | Detailed retrieval traces for debugging |
| `0029` | `evidence_packets` | Evidence packet records | `evidence_packets` | Versioned evidence packets for synthesis |
| `0030` | `duplicate_groups` | Near-duplicate detection groups | `evidence_duplicate_groups` | Deduplication in evidence construction |

### Phase 7 — Synthesis, cache, resource governance

| Revision | Name | Description | Tables added | Impact |
|----------|------|-------------|--------------|--------|
| `0031` | `synthesis_stages` | Bounded autonomous-local synthesis | `synthesis_stages` | Stage-level synthesis tracking (outline, binding, draft, citation_pass) |
| `0032` | `semantic_cache` | Semantic result caching | `semantic_cache`, `semantic_cache_events` | Cached semantic results with revalidation |
| `0033` | `resource_governance` | Model endpoint health tracking | `model_endpoints` | Endpoint health, concurrency, and backpressure tracking |

## 5. Interrupted migration repair

### 5.1 Transactional rollback (automatic)

Because Alembic DDL runs in a single PostgreSQL transaction:

- If the migration process is killed or crashes, PostgreSQL rolls back all DDL.
- The schema remains at the previous revision.
- `research-db status` shows the prior revision as current.
- **Action:** Simply rerun `research-db migrate`.

### 5.2 Schema claims a revision but objects are absent

If `alembic_version` reports a revision but required tables/columns are missing:

1. **Do not hand-create objects.**
2. Restore the pre-migration PostgreSQL backup.
3. Rerun `research-db migrate`.

If the issue persists:

1. Inspect the migration file for the specific revision.
2. Add a forward-repair migration that creates the missing objects.

### 5.3 Partial schema_migrations entry

If `schema_migrations` has a version entry but `alembic_version` does not (or vice versa):

1. Check which tables/objects exist.
2. Align the version tracking by editing `alembic_version` to match the actual schema.
3. **Or** restore from backup and rerun.

## 6. Forward-repair migrations

When a migration fails partially or the schema is in an inconsistent state, create a forward-repair migration:

1. **Identify the gap:** What tables/columns/indexes are missing or incorrect?
2. **Write the migration:** Use `IF NOT EXISTS` guards and idempotent DDL.
3. **Test on a disposable database.**
4. **Apply to production.**

Example forward-repair migration:

```python
"""Forward repair: add missing idx_research_events_run_cursor_idx.

This migration repairs a missing index from v0006 that was not created
due to a race condition during concurrent migration application.
"""

from alembic import op

revision = "0034_repair_event_index"
down_revision = "0033_resource_governance"


def upgrade():
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_indexes WHERE tablename = 'research_events'
            AND indexname = 'research_events_run_cursor_idx'
          ) THEN
            CREATE INDEX research_events_run_cursor_idx
              ON research_events(run_id, created_at, id);
          END IF;
        END $$;
        """
    )
    op.execute(
        "INSERT INTO schema_migrations(version) VALUES (34) ON CONFLICT DO NOTHING"
    )


def downgrade():
    raise RuntimeError("Forward-only repair migration")
```

## 7. Migration testing

### 7.1 Fresh migration test

```bash
# Create a fresh disposable database
createdb firecrawl_test_fresh

export DATABASE_URL='postgresql://research_app:password@localhost/firecrawl_test_fresh'

# Run migrations
rtk proxy "<skill-root>/scripts/research-db" migrate

# Verify
rtk proxy "<skill-root>/scripts/research-db" status
rtk proxy "<skill-root>/scripts/research-db" ingest-ready
rtk proxy "<skill-root>/scripts/research-db" doctor
```

### 7.2 Upgrade from prior head

```bash
# Create a database at the prior revision
createdb firecrawl_test_upgrade
export DATABASE_URL='postgresql://research_app:password@localhost/firecrawl_test_upgrade'

# Apply migrations up to the prior head (manually set alembic_version)
# Then upgrade to current head
rtk proxy "<skill-root>/scripts/research-db" migrate

# Verify all objects exist
rtk proxy "<skill-root>/scripts/research-db" doctor
```

### 7.3 Populated database test

```bash
# Create a database with existing corpus data
createdb firecrawl_test_populated
export DATABASE_URL='postgresql://research_app:password@localhost/firecrawl_test_populated'

# Import existing scratch data
rtk proxy "<skill-root>/scripts/research-db" import-scratch "$SCRATCH_ROOT"

# Run migrations
rtk proxy "<skill-root>/scripts/research-db" migrate

# Verify corpus data is preserved
rtk proxy "<skill-root>/scripts/research-db" corpus-overview
rtk proxy "<skill-root>/scripts/research-db" verify-blobs
```

### 7.4 Integration test suite

```bash
# Point at a uniquely named disposable database
export RESEARCH_STORE_TEST_ALLOW_RESET='firecrawl_test_migration'
export DATABASE_URL='postgresql://research_app:password@localhost/firecrawl_test_migration'

# Run the integration test suite
env PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider \
  "<skill-root>/scripts/test_research_store_integration.py"
```

This test suite proves:

- Non-empty prior-to-current Alembic upgrade.
- Concurrent idempotent ingestion.
- Active derivations.
- Invocation-ledger replacement.
- Run immutability.
- Expired-final-attempt recovery.
- Stale-token rejection.
- Manifest-definition binding.

---

*Cross-reference `research-store-operations.md` for the operational migration procedure and `operations-runbook.md` for the full disaster-recovery workflow.*
