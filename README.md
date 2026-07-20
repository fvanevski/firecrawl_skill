# Firecrawl Codex CLI Skill

An advanced web research skill for Codex CLI with a PostgreSQL-centered research corpus and a filesystem compatibility workflow. It provides context-efficient searching, page scraping, LLM-guided planning, targeted passage retrieval, and persistent audit cataloging.

## Overview

Large context windows are easily overwhelmed by raw web search results and unparsed web pages. This skill isolates raw acquisition data using structured scratch directories (`firecrawl_scratch/fc_<uuid>/`) for filesystem compatibility, while automatically persisting structured snapshots, documents, and chunks into an authoritative PostgreSQL research store when `DATABASE_URL` is configured.

Additionally, the skill features **Catalog Schema v5**—a persistent audit system that records objective briefs, execution telemetry, mechanical metrics, candidate provenance, and staged LLM semantic quality audits without retaining full page bodies or sensitive credentials.

---

## Core Features

- **Disk-Backed Context Efficiency**: Writes search results, scraped markdown, and candidate ledgers to local scratch files, preserving LLM context for synthesis.
- **Authoritative Research Store (`research-db`)**: Persists immutable snapshots, structural blocks, chunks, provenance, research runs, retrieval logs, and transactional index jobs in PostgreSQL; large raw payloads use content-addressed blobs (`BLOB_ROOT`).
- **Hybrid Manifest-First Retrieval**: Combines PostgreSQL full-text search (FTS) with Qdrant dense vector search, reciprocal-rank fusion (RRF), and local reranking. Returns compact candidate manifests before bounded, citation-ready passages.
- **LLM-Planned Adaptive Research (`fsearch_smart`)**: Automatically classifies objectives (`simple`, `moderate`, `complex`), generates structured research briefs, executes adaptive candidate search waves, triages source quality, and collects structured evidence (`_evidence.json`).
- **Granular CLI Wrappers (`fsearch`, `fscrape`)**: Run targeted single-query searches with bounded candidate scraping, or extract structured JSON schema data directly from URLs.
- **Selective File & Directory Inspector (`fread`)**: List search history, view scratch indexes, search/grep scratch directories, and inspect specific file line ranges (`--skip` / `--lines`).
- **Research Run Lifecycle Management (`frun`)**: Group multi-step investigations into explicit research runs (`fr_<uuid>`), manage state (`start`, `finish`, `reopen`, `annotate`), perform local/remote LLM quality audits, and aggregate performance metrics.
- **Automated Markdown Post-Processing (`cleanup.py`)**: Normalizes whitespace, strips website navigation boilerplate, removes tracking URL parameters, and simplifies markdown image references.

---

## Repository Structure

```text
firecrawl/
├── SKILL.md                              # Core skill definition and workflow rules for Codex
├── README.md                             # Repository overview and documentation
├── .gitignore                            # Python, virtualenv, cache, and secrets exclusion rules
├── alembic.ini                           # Alembic database schema migration configuration
├── requirements-research-store.txt       # Python dependencies for the Authoritative Research Store
├── agents/
│   └── openai.yaml                       # Agent model provider configuration template
├── references/
│   ├── catalog-policy-v5.json            # Catalog Schema v5 policy specification
│   ├── catalog-v5.md                     # Deep dive documentation on Catalog v5 auditing
│   ├── cli-script-disambiguation.md      # Disambiguation guide for Firecrawl CLI vs Python SDK vs MCP
│   ├── research-store-architecture.md    # Architecture specification for Authoritative Research Store
│   └── research-store-operations.md      # Deployment, migration, backup/restore, and recovery operations
└── scripts/
    ├── fsearch_smart                     # Executable: Adaptive, LLM-planned research orchestrator
    ├── fsearch                           # Executable: Single-query Firecrawl search wrapper
    ├── fscrape                           # Executable: URL page scraper with boilerplate cleanup
    ├── fread                             # Executable: Inspection tool for scratch files and catalog
    ├── frun                              # Executable: Explicit research run lifecycle manager
    ├── research-db                       # Executable: Authoritative PostgreSQL corpus operations & retrieval CLI
    ├── research-env                      # Environment loader script for Research Store configurations
    ├── persist_results.py                # Wrapper persistence service backend (meta.json -> PostgreSQL)
    ├── research_store/                   # Domain models, repositories, services, migrations, indexing
    ├── catalog_v5.py                     # Catalog storage, telemetry, and audit log engine
    ├── classifier.py                     # Research complexity classifier & strategy generator
    ├── cleanup.py                        # Markdown post-processing & boilerplate removal
    ├── invocation_catalog.py             # CLI backend interface for catalog operations
    ├── invocation_id.py                  # Invocation UUID generator (fc_<uuid>, fr_<uuid>)
    ├── live_validate.py                  # Live API endpoint validation harness
    ├── model_gateway.py                  # LLM provider gateway (Local models, OpenAI)
    ├── research_workflow.py              # Multi-step research workflow engine
    ├── test_classifier.py                # Unit tests for classifier logic
    ├── test_workflow.py                  # Unit test suite for workflow & catalog
    ├── test_research_store.py            # Unit tests for Research Store domain & services
    └── test_research_store_integration.py# Integration tests for live PostgreSQL & Qdrant environments
```

---

## Installation & Prerequisites

1. **Python Runtime**: Python 3.10 or higher.
2. **Firecrawl CLI / API**: Ensure `firecrawl` CLI is installed and accessible in your system path, or configured against a self-hosted instance.
3. **Research Store Dependencies (Optional)**: Install `requirements-research-store.txt` if using PostgreSQL persistence and vector search capabilities:
   ```bash
   pip install -r requirements-research-store.txt
   ```
4. **Command Execution with RTK**: Wrap command executions through `rtk proxy` at the agent-visible boundary (e.g. `rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>"`).

---

## Usage Guide

### 1. Authoritative Research Store (`research-db`)

When `DATABASE_URL` is set, `fsearch` and `fscrape` automatically persist results to PostgreSQL, producing `_corpus.json` manifests. Query and manage the authoritative corpus with `research-db`:

```bash
# Apply database schema migrations
rtk proxy "<skill-root>/scripts/research-db" migrate

# Verify service connectivity (Postgres, Blob Store, Qdrant, Valkey)
rtk proxy "<skill-root>/scripts/research-db" doctor

# View corpus overview and statistics
rtk proxy "<skill-root>/scripts/research-db" corpus-overview

# Hybrid search over research assets (FTS + Qdrant Dense + RRF + Reranker)
rtk proxy "<skill-root>/scripts/research-db" search-assets "<query>" --limit 20

# Inspect asset metadata & structure by ID
rtk proxy "<skill-root>/scripts/research-db" inspect-asset "<candidate-id>"

# Fetch citation-ready passages bounded by token count
rtk proxy "<skill-root>/scripts/research-db" fetch-passages "<candidate-id>" --max-tokens 2000

# Import legacy scratch directory trees into the database
rtk proxy "<skill-root>/scripts/research-db" import-scratch "<scratch-dir>" --dry-run
rtk proxy "<skill-root>/scripts/research-db" import-scratch "<scratch-dir>"
```

### 2. Adaptive Smart Search (`fsearch_smart`)

The recommended entry point for multi-faceted or complex topics. Automatically formulates brief goals, query strategies, candidate triaging, and evidence aggregation:

```bash
# Auto-classify complexity and orchestrate research
rtk proxy "<skill-root>/scripts/fsearch_smart" "Latest developments in quantum computing"

# Explicit complexity & scope filters
rtk proxy "<skill-root>/scripts/fsearch_smart" "Python 3.12 GIL changes" --complexity moderate --tbs qdr:m

# Dry-run heuristic strategy planning
rtk proxy "<skill-root>/scripts/fsearch_smart" "PostgreSQL 16 performance features" --complexity complex --planner heuristic --dry-run
```

### 3. Single-Query Search (`fsearch`)

For focused, single-query lookups with candidate scraping caps:

```bash
# Search 20 candidates, scrape top 5
rtk proxy "<skill-root>/scripts/fsearch" "Firecrawl Python SDK usage" --limit 20 --scrape-limit 5

# Metadata-only candidate discovery (no page scraping)
rtk proxy "<skill-root>/scripts/fsearch" "Web scraping best practices" --limit 50 --scrape-limit 0
```

### 4. URL Scraping (`fscrape`)

Scrape individual pages to clean markdown scratch files or force JSON schema extraction:

```bash
# Scrape pages to markdown
rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/article1" "https://example.com/article2"

# Structured extraction with JSON schema
rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/product" \
  --schema '{"type":"object","properties":{"name":{"type":"string"},"price":{"type":"string"}},"required":["name","price"]}'
```

### 5. Scratch File & History Inspection (`fread`)

Inspect results without loading massive raw web text into context:

```bash
# Show search history
rtk proxy "<skill-root>/scripts/fread" --history

# Inspect scratch directory index & candidates
rtk proxy "<skill-root>/scripts/fread" "<scratch-dir>"

# Search text across scratch files
rtk proxy "<skill-root>/scripts/fread" "<scratch-dir>" --grep "benchmark"

# Read specific line slice from a scraped file
rtk proxy "<skill-root>/scripts/fread" "<scratch-dir>/result_001.md" --skip 30 --lines 50
```

### 6. Multi-Step Research Runs & Auditing (`frun`)

Group multi-phase investigations into durable audit records (`fr_<uuid>`):

```bash
# 1. Start explicit research run
RUN_ID="$(rtk proxy "<skill-root>/scripts/frun" start "Investigate vector database performance benchmark" --profile auto)"

# 2. Attach operations to research run
rtk proxy "<skill-root>/scripts/fsearch_smart" "Qdrant vs Milvus vs pgvector benchmark" --research-run-id "$RUN_ID"
rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/benchmark-report" --research-run-id "$RUN_ID"

# 3. Complete research run with source manifest
rtk proxy "<skill-root>/scripts/frun" finish "$RUN_ID" --outcome satisfied --source-manifest sources.json --answer-file final.md

# 4. View catalog summary and perform LLM quality audit
rtk proxy "<skill-root>/scripts/fread" --catalog "$RUN_ID"
rtk proxy "<skill-root>/scripts/frun" audit "$RUN_ID" --llm local
```

---

## Configuration & Environment Variables

| Variable | Description | Default |
| --- | --- | --- |
| `FIRECRAWL_API_KEY` | Firecrawl API key | *(Optional if self-hosted)* |
| `FIRECRAWL_API_URL` | Firecrawl service endpoint URL | `http://localhost:3002` |
| `FIRECRAWL_CATALOG_DIR` | Custom directory path for catalog audit logs | `${XDG_DATA_HOME:-~/.local/share}/firecrawl/` |
| `FIRECRAWL_CATALOG_DISABLED` | Set to `1` to disable persistent catalog audit logging | `0` |
| `FIRECRAWL_AUDIT_AUTO_SEMANTIC` | Set to `0` to disable automatic post-run LLM semantic auditing | `1` |
| `DATABASE_URL` | PostgreSQL DSN; enables authoritative wrapper persistence | *(filesystem compatibility only)* |
| `BLOB_ROOT` | Content-addressed immutable payload storage directory | `~/.local/share/firecrawl/blobs` |
| `QDRANT_URL` / `QDRANT_COLLECTION` | Rebuildable retrieval projection endpoint and collection | `http://localhost:6333` / `research_chunks_v1` |
| `VALKEY_URL` | Transient queue and cache backend URL | `redis://localhost:6379/0` |
| `EMBEDDING_URL` / `EMBEDDING_MODEL` | OpenAI-compatible dense embedding endpoint and model | `http://127.0.0.1:8004/v1/embeddings` / `embed` |
| `RERANKER_URL` / `RERANKER_MODEL` | Cohere-compatible bounded reranker endpoint and model | `http://127.0.0.1:8004/v1/rerank` / `rerank` |

See `references/research-store-architecture.md` and `references/research-store-operations.md` for detailed specifications on database schemas, migrations, deployment, backup/restore, recovery, and evaluation procedures.

---

## Testing & Verification

### Unit Tests
Run the deterministic unit test suite via `pytest`:

```bash
rtk pytest -q -p no:cacheprovider scripts/test_classifier.py scripts/test_workflow.py scripts/test_research_store.py
```

### Integration Tests
Run integration tests for live PostgreSQL and Qdrant environments:

```bash
rtk pytest -q -p no:cacheprovider scripts/test_research_store_integration.py
```

### Live Validation Campaign
Run bounded live endpoint verification against an active Firecrawl API:

```bash
rtk proxy "<skill-root>/scripts/live_validate.py" \
  --api-url "${FIRECRAWL_API_URL:-http://localhost:3002}" \
  --max-operations 125 --planner both
```
