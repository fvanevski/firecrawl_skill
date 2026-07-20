# Research Store Operations

## Responsibilities and source-of-truth rules

PostgreSQL owns sources, immutable snapshots, normalized documents, ordered blocks, deterministic chunks, relations, runs, retrieval events, embedding manifests, and index jobs. The blob root owns immutable large/raw payload bytes referenced by snapshot hash. Qdrant is a disposable retrieval projection. Valkey holds transient wakeups/cache only. Scratch and exports are compatibility and audit artifacts.

The schema is in `scripts/research_store/migrations/001_initial.sql` and is applied through Alembic. The service transaction writes source/snapshot/document/block/chunk rows and the indexing outbox together. Qdrant is updated only by an outbox worker after the commit.

## Configuration

Required for corpus persistence:

```bash
export DATABASE_URL='postgresql://research:...@localhost/research'
export BLOB_ROOT="$HOME/.local/share/firecrawl/blobs"
```

On the configured host, `scripts/research-env` derives authenticated loopback URLs from the file-backed secrets under `/opt/containers/research-{postgres,qdrant,valkey}`. `research-db`, `fsearch`, and `fscrape` source it automatically when those files exist. Set `FIRECRAWL_RESEARCH_AUTO_ENV=0` to disable this adapter or override the `RESEARCH_*_DIR` and endpoint variables for another installation. Secrets are never copied into the skill tree or command output.

Optional configuration: `QDRANT_URL`, `QDRANT_API_KEY`, `QDRANT_COLLECTION`, `VALKEY_URL`, `SCRATCH_ROOT`, `EMBEDDING_URL`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`, `EMBEDDING_REVISION`, `EMBEDDING_DIMENSION`, `RERANKER_URL`, `RERANKER_API_KEY`, `RERANKER_MODEL`, `RERANKER_CANDIDATE_LIMIT`, `CHUNKER_VERSION`, and `PARSER_VERSION`. Never commit credentials. Install the pinned Python integration set from `requirements-research-store.txt` in an isolated environment.

The host adapter uses `http://127.0.0.1:8004/v1/embeddings` with proxy alias `embed` (Qwen3-Embedding-0.6B Q6_K, 1024 dimensions) and `http://127.0.0.1:8004/v1/rerank` with alias `rerank` (bge-reranker-v2-m3 Q6_K). Full endpoint URLs are accepted; clients do not append a duplicate route.

## Local Arch Linux deployment

This host uses three independently managed, version-pinned Compose projects at `/opt/containers/research-postgres`, `/opt/containers/research-qdrant`, and `/opt/containers/research-valkey`. Their application ports are loopback-bound and they also join the external `agent-search` network. Keep their file-backed secrets mode 0600, review upgrades deliberately, and do not bind the corpus database lifecycle to Arch's rolling PostgreSQL packages.

```bash
docker compose -f /opt/containers/research-postgres/docker-compose.yaml up -d
docker compose -f /opt/containers/research-qdrant/docker-compose.yaml up -d
docker compose -f /opt/containers/research-valkey/docker-compose.yaml up -d
rtk proxy scripts/research-db migrate
rtk proxy scripts/research-db doctor
```

## Data lifecycle and versioning

Canonical URL establishes logical source identity. Every distinct content hash creates an immutable snapshot; same source/hash is idempotently reused. A changed snapshot points to its prior snapshot. Different sources may reference one deduplicated blob hash while retaining separate source identities. Small normalized text remains in PostgreSQL. Relation classes are constrained to `observed`, `source_asserted`, and `model_inferred`; inferred relations require extraction provenance.

Do not delete a blob while any `asset_snapshots.raw_blob_uri` references it. Retention should first delete eligible relational records in a reviewed transaction, then garbage-collect only unreferenced verified hashes. No automatic blob deletion is implemented.

## Migration and compatibility

```bash
rtk proxy scripts/research-db import-scratch "$SCRATCH_ROOT" --dry-run --report /tmp/import-dry.json
rtk proxy scripts/research-db import-scratch "$SCRATCH_ROOT" --report /tmp/import.json
```

The importer recognizes numbered search/scrape assets referenced by `_meta.json`, hashes all payloads, retains the old path in migration metadata, continues after individual failures, and is idempotent through `(source_id, content_sha256)`. Existing wrapper outputs remain enabled. With `DATABASE_URL` set, each wrapper explicitly persists its results and emits `_corpus.json` stable IDs.

## Retrieval and exports

Agent operations are manifest-first: `corpus-overview`, `search-assets`, and `inspect-asset` return metadata; `fetch-passages` applies hard passage/token limits. Relationship expansion is hard-limited to three hops. Full unrestricted document retrieval is intentionally absent.

```bash
rtk proxy scripts/research-db corpus-overview
rtk proxy scripts/research-db search-assets 'query' --limit 20 --domain example.org
rtk proxy scripts/research-db inspect-asset <chunk-id>
rtk proxy scripts/research-db fetch-passages <chunk-id> --max-tokens 2000
rtk proxy scripts/research-db export-run <run-id> --output /tmp/run.json
```

Exports are non-authoritative debugging/audit copies.

## Backup, restore, rebuild, and recovery

Back up PostgreSQL and the blob root at a mutually consistent recovery point. Stop ingestion or record a database snapshot boundary, run `pg_dump --format=custom`, and snapshot/archive `BLOB_ROOT`. Qdrant and Valkey do not need authoritative backups.

Restore PostgreSQL and blobs first, run `verify-blobs`, recreate the Qdrant collection, then schedule every chunk for reindexing:

```bash
rtk proxy scripts/research-db verify-blobs
rtk proxy scripts/research-db reindex --all
rtk proxy scripts/research-db index-once --limit 64
rtk proxy scripts/research-db reconcile-qdrant
rtk proxy scripts/research-db doctor
```

Qdrant outages leave failed/pending jobs in PostgreSQL with exponential retry/dead-letter state. Valkey loss only loses wakeups/cache; workers recover work by polling `index_jobs`. Use `reindex --document <uuid>` for a targeted rebuild. `doctor` checks PostgreSQL/schema, blob permissions and references, Qdrant schema, Valkey, the embedding endpoint, job health, and configured model/parser/chunker versions. `reconcile-qdrant` reports missing and orphaned point IDs; it does not destructively repair them.

## Retrieval evaluation

Use a fixed corpus/query fixture and store candidate IDs plus relevance judgments. Compare lexical-only, semantic-only, fusion, and reranked runs for recall, duplicate suppression, source diversity, citation/source/snapshot correctness, delivered tokens, and relevant-evidence density. Do not claim an improvement until the fixture produces measured results. Parser, chunker, embedding, or policy changes require a new version and a regression comparison.
