# PostgreSQL Migration Guide

## 1. Migration principles

PostgreSQL is the only schema authority. Alembic migrations are forward-only and execute transactionally. Blob storage, Qdrant, Valkey, and scratch directories do not determine schema state.

The current clean schema head is `0038_postgres_authority`. The deprecated filesystem workflow mirror and adapter tables were removed from the migration chain. Databases initialized from the older chain are intentionally unsupported; reset the research datastore rather than attempting an in-place upgrade.

## 2. Pre-migration checklist

1. Confirm the checkout and configuration:

   ```bash
   git status --short
   scripts/research-db status
   scripts/research-db doctor
   ```

2. For a new deployment or any database created by an older release, run the guarded clean reset:

   ```bash
   scripts/reset-firecrawl-research
   ```

3. For a supported current-schema deployment, capture PostgreSQL and blob recovery boundaries before applying a future forward migration.

4. Stop acquisition writers and the persistent index worker before destructive reset or schema repair.

## 3. Running migrations

```bash
scripts/research-db migrate
scripts/research-db status
scripts/research-db ingest-ready
scripts/research-db doctor
```

Expected status:

```json
{
  "current": "0038_postgres_authority",
  "head": "0038_postgres_authority",
  "at_head": true
}
```

Do not stamp Alembic manually, edit `alembic_version`, or hand-create missing objects. A successful migration is not sufficient by itself; `ingest-ready` and `doctor` must also pass.

## 4. Migration sequence

The clean chain deliberately contains gaps where removed deprecated revisions once existed. Revision identifiers remain stable for the retained PostgreSQL features.

| Revision | Purpose |
|---|---|
| `0001_research_store` | Core sources, snapshots, documents, chunks, embedding manifests, jobs, and retrieval events |
| `0002_research_store_integrity` | Content-addressed integrity, index definitions, batches, run assets, leases, and active-index lifecycle |
| `0003_job_manifest_integrity`, `0004_manifest_definition_key` | Job/manifest constraints before workflow-state creation |
| `0006_workflow_state` | Authoritative runs, invocations, events, specifications, semantic calls/artifacts, and transition ledger |
| `0007_budget_snapshots` | Immutable per-run resource authorization snapshots |
| `0009_search_plans`–`0016_invocation_events` | Search planning, raw response retention, candidate identity, coverage, terminal decisions, and ordered invocation events |
| `0017_claims_evidence`–`0019_audit_identity` | Claims, evidence bindings, staged audits, and audit identity |
| `0021_extraction_attempts`–`0030_duplicate_groups` | Extraction provenance, parsers, derivations, retrieval traces, evidence packets, and duplicate groups |
| `0031_synthesis_stages`, `0032_semantic_cache`, `0033_resource_governance`, `0034_add_validation_stage` | Synthesis, semantic cache, resource governance, and validation |
| `0035_index_point_counts`–`0037_not_invoked_token_source` | Index verification and measured performance telemetry |
| `0038_postgres_authority` | Current PostgreSQL-only schema head and resource-sample completeness |

Removed revisions are not migration targets and must not be recreated.

## 5. Interrupted migration repair

Alembic DDL runs in a transaction. After interruption:

```bash
scripts/research-db status
scripts/research-db migrate
scripts/research-db status
```

When `current` remains at the prior revision, rerun the migration. When Alembic reports the new revision but required objects are absent, restore/reset rather than editing the schema manually.

For this PostgreSQL-only baseline, an old or ambiguous database should be replaced with:

```bash
scripts/reset-firecrawl-research --yes
```

The reset removes PostgreSQL, Qdrant, Valkey, and blob data. It is appropriate only when those assets are disposable.

## 6. Forward-repair migrations

Future corrections must be new Alembic revisions. A forward repair must:

- preserve PostgreSQL as the only workflow authority;
- avoid filesystem-derived state;
- keep transition and event ledgers append-only;
- make retries idempotent;
- document recovery and validation;
- include fresh-database and populated-prior-head tests when in-place upgrades are supported.

Do not revise a migration that has already shipped as part of a supported schema lineage. This repository’s current clean baseline is an explicit exception because older databases are declared disposable and require reset.

## 7. Migration testing

Run migration and schema tests against an explicitly disposable database:

```bash
export RESEARCH_STORE_TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1/firecrawl_test'
export RESEARCH_STORE_TEST_ALLOW_RESET='firecrawl_test'

env PYTHONDONTWRITEBYTECODE=1 \
  pytest -q -p no:cacheprovider \
  scripts/test_research_store_integration.py \
  scripts/test_workflow_service.py
```

Then verify service boundaries:

```bash
scripts/research-db verify-blobs
scripts/research-db index-build --current-config --all
scripts/research-db worker --once --batch-size 64
scripts/research-db reconcile-qdrant
scripts/research-db doctor
```

A release candidate is not migration-ready until the exact head passes Python 3.11 and 3.12 CI, Ruff, the disposable PostgreSQL integration suite, and the wrapper workflow smoke test.
