# Research Asset Store Architecture

## Before migration

| Boundary | Current behavior |
| --- | --- |
| Crawl entry points | `fsearch`, `fscrape`, and `fsearch_smart` invoke the Firecrawl CLI. |
| Response formats | Search writes the raw response to `_search.json`; scrape writes numbered Markdown or JSON payloads. `_meta.json`, `_context.json`, `_candidates.json`, and `_index.md` describe them. |
| State | Scratch files and catalog-v5 JSON records are the only durable/queryable records. Agent retrieval walks files with `fread`. |
| Naming | `firecrawl_scratch/fc_<uuid>/{search,scrape,smart}` with `result_NNN.*`, `url_NNN.*`, and underscore-prefixed manifests. |
| Transformations | Firecrawl response -> cleanup -> numbered file -> manifest; smart search adds candidate triage and evidence manifests. No structural parser, deterministic chunker, or embedding stage exists. |
| Failure behavior | Wrapper retries and scrape fallbacks are bounded. Errors are retained in manifests/catalog events; successful corpus state and pending indexing work are not transactionally recorded. |
| Existing services | No PostgreSQL, Qdrant, or Valkey client exists in the skill. |

These contracts are covered by `scripts/test_workflow.py` and remain available through the filesystem compatibility adapter.

## Target data flow

```text
Firecrawl result
  -> CorpusService transaction
     -> PostgreSQL: source -> immutable snapshot -> document -> blocks -> chunks
     -> PostgreSQL: index_jobs (same commit)
     -> content-addressed blob store (large/raw immutable payload)
  -> optional scratch compatibility export

Index worker
  -> claims PostgreSQL index_job
  -> loads canonical chunk from PostgreSQL
  -> generates versioned embedding
  -> upserts Qdrant projection
  -> updates embedding_manifest and job

Agent
  -> corpus_overview/search_assets (manifests only)
  -> inspect_asset/fetch_passages/build_evidence_packet (bounded expansion)
  -> retrieval_events in PostgreSQL
```

PostgreSQL is authoritative. Blob files are immutable payload storage referenced by PostgreSQL. Qdrant is rebuildable. Valkey is optional transient coordination only. Scratch directories are temporary processing, compatibility, and export surfaces.

## Consistency and recovery

- Corpus records and index jobs commit in one PostgreSQL transaction (transactional outbox); there is no PostgreSQL/Qdrant dual write.
- A Qdrant failure leaves the corpus committed and the job retryable/dead-lettered.
- Snapshot bytes are content-addressed and immutable. Same-source/same-hash ingestion is idempotent; changed content links to the previous snapshot.
- Queue messages contain stable PostgreSQL job IDs. Clearing Valkey does not remove pending work.
- Qdrant can be dropped and rebuilt from chunks and embedding manifests.

