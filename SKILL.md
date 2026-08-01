---
name: firecrawl
description: "Acquire, retain, retrieve, and audit web research with Firecrawl. Use when Codex needs to search or scrape the web, query or inspect the authoritative PostgreSQL research corpus, run hybrid lexical/vector retrieval, fetch bounded citation passages, preserve scratch artifacts, diagnose ingestion or indexing, or manage research provenance and recovery."
---

<!-- @format -->

# Firecrawl Research Corpus and Acquisition

PostgreSQL is the sole authority for research workflow state, invocations, claims, audits, and corpus identities. Content-addressed blobs retain immutable payloads, Qdrant is a rebuildable vector projection, Valkey provides transient wakeups, and scratch directories are disposable diagnostics. Use the database first for retained research. Use Firecrawl acquisition wrappers when the corpus lacks current evidence, then retrieve through compact database manifests and bounded passages.

## Choose the First Operation

1. Run `research-db corpus-overview` and `search-assets "<query>"` for retained-corpus questions.
2. Inspect promising candidate manifests, then call `fetch-passages` with a token bound. Do not preload full documents.
3. Run `fsearch_smart`, `fsearch`, or `fscrape` only when the corpus is empty, stale, incomplete, or the request explicitly requires new web acquisition.
4. Inspect the invocation or research-run record before retrying failed or weak acquisition. Distinguish acquisition, persistence, indexing, and retrieval failures.
5. Use `fread` only for wrapper diagnostics, scratch history, and filesystem-only/private runs.

```bash
rtk proxy "<skill-root>/scripts/research-db" corpus-overview
rtk proxy "<skill-root>/scripts/research-db" search-assets "<query>" --limit 20
rtk proxy "<skill-root>/scripts/research-db" inspect-asset "<candidate-id>"
rtk proxy "<skill-root>/scripts/research-db" fetch-passages "<candidate-id>" --max-tokens 2000
```

## Resource Layout

Resolve paths relative to this skill root. Do not assume the skill lives under a specific home directory or that the current working directory is the skill directory. Launch every bundled wrapper through `rtk proxy` so Codex hook enforcement is satisfied while complete progress and scratch-directory output remain visible. Replace `<skill-root>` with the directory containing this `SKILL.md`.

```bash
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>"
rtk proxy "<skill-root>/scripts/fsearch" "<query>" --limit 20 --scrape-limit 5
rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/article"
rtk proxy "<skill-root>/scripts/fread" --history
```

Keep `rtk proxy` at the outer agent-visible boundary. Do not add RTK inside the wrappers: their direct `firecrawl` subprocesses write and inspect artifact files and must retain unmodified streams and exit codes.

The scripts write scratch output below the platform temporary directory's `firecrawl_scratch/` subdirectory unless a custom output directory is provided. They honor `TMPDIR` and otherwise use Python's platform-aware temporary-directory lookup, which works on conventional Linux and Android Termux layouts.

Generate one `fc_<uuid>` invocation ID for every top-level wrapper run. Use it as the scratch root and persist it in every generated orientation or metadata artifact (`_index.md`, `_meta.json`, and `_context.json` where applicable). A smart search propagates the same ID to every subquery so its hierarchy remains unambiguous:

```text
<platform-temp>/firecrawl_scratch/
├── fc_<uuid-a>/
│   └── search/
├── fc_<uuid-b>/
│   └── scrape/
└── fc_<uuid-c>/
    └── smart/
        ├── query_01/
        ├── query_02/
        └── query_NN/
```

Treat `fc_<uuid>` as the research invocation boundary, `search|scrape|smart` as the operation, and `query_NN` as smart-search branch provenance. Preserve explicit `--dir` or `--output-dir` paths for artifact reuse, but still record the generated or inherited invocation ID in their metadata. Use `--invocation-id` only to deliberately attach a wrapper call to an existing invocation.

## PostgreSQL Workflow Provenance

Every persistent top-level operation belongs to an explicit `fr_<uuid>` run and records an authoritative `fc_<uuid>` invocation in PostgreSQL. Wrapper startup validates the run before network acquisition, records the invocation, and advances only permitted lifecycle stages. Wrapper completion records the terminal invocation status and advances to indexing only after corpus persistence succeeds. Scratch metadata is useful for inspection but is never read to determine run or invocation state.

```bash
RUN_ID="$(rtk proxy "<skill-root>/scripts/frun" start "<research objective>" --mode autonomous_local)"
rtk proxy "<skill-root>/scripts/fsearch" "<query>" --research-run-id "$RUN_ID"
rtk proxy "<skill-root>/scripts/fscrape" "https://primary.example" --research-run-id "$RUN_ID"
rtk proxy "<skill-root>/scripts/research-db" run-status "$RUN_ID"
rtk proxy "<skill-root>/scripts/research-db" doctor
rtk proxy "<skill-root>/scripts/frun" finish "$RUN_ID" --outcome satisfied --source-manifest sources.json --answer-file final.md
```

The valid low-level path is `created → planning → corpus_review → acquiring → extracting → indexing`. Once all run-scoped index jobs complete, `frun finish` advances `indexing → coverage_review → synthesizing → validating → completed`. Failed and partial outcomes use only state-machine-permitted terminal transitions. Retry uncertain commands with the same idempotency key; after a stale-revision rejection, inspect `run-status` before issuing new work.

Finished runs are immutable. Reopen one explicitly before attaching more work, and annotate material pivots:

```bash
rtk proxy "<skill-root>/scripts/frun" reopen "$RUN_ID" --reason "add missing official corroboration"
rtk proxy "<skill-root>/scripts/frun" annotate "$RUN_ID" --type pivot --reason "switched to direct official URLs"
rtk proxy "<skill-root>/scripts/frun" verify "$RUN_ID"
rtk proxy "<skill-root>/scripts/frun" audit "$RUN_ID" --llm local
rtk proxy "<skill-root>/scripts/frun" audit-status "$RUN_ID"
rtk proxy "<skill-root>/scripts/frun" compare "$RUN_ID" "$OTHER_RUN_ID"
```

No filesystem workflow mirror or adapter mode exists. See `references/workflow-state-schema.md` for the PostgreSQL lifecycle and `references/operations-runbook.md` for recovery.

## Authoritative Research Asset Store

PostgreSQL is authoritative; content-addressed blobs retain immutable payload bytes; versioned Qdrant collections are rebuildable retrieval projections; Valkey provides optional wakeups only. Scratch files are disposable diagnostics. Successful persistent wrappers create an authoritative invocation, persist an ingestion batch, write `_corpus.json` with stable source, snapshot, document, and chunk IDs, and attach those records to `--research-run-id fr_<uuid>`.

Set `FIRECRAWL_RESEARCH_PERSIST=auto|on|off`. Use `auto` to persist when a database resolves, `on` to require persistence before acquisition begins, and `off` for filesystem-only acquisition. Persistence is fail-closed: if an enabled store cannot retain every successful Firecrawl result, preserve diagnostic scratch output and the partial corpus manifest, then return nonzero. Never silently downgrade to scratch-only mode. For private scratch-only runs, disable research persistence:

```bash
FIRECRAWL_RESEARCH_PERSIST=off \
  rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/private"
```

Use manifest-first database operations for retained research:

```bash
rtk proxy "<skill-root>/scripts/research-db" corpus-overview
rtk proxy "<skill-root>/scripts/research-db" search-assets "<query>" --limit 20
rtk proxy "<skill-root>/scripts/research-db" inspect-asset "<candidate-id>"
rtk proxy "<skill-root>/scripts/research-db" fetch-passages "<candidate-id>" --max-tokens 2000
```

`search-assets` selects only the configured parser, normalizer, and chunker derivations. It combines PostgreSQL full-text candidates with the active Qdrant alias, reciprocal-rank fusion, and the configured local reranker when the alias targets the exact configured embedding fingerprint. On a fingerprint mismatch it skips query embedding and falls back to lexical retrieval; `doctor` reports the mismatch as unhealthy. Candidate manifests expose lexical, semantic, fused, and reranker scores without preloading full documents. Physical collections use the embedding-definition fingerprint; switch `research_chunks_active` only after the replacement index is complete and verified.

Run the durable lease-safe worker as a persistent user-systemd service. PostgreSQL jobs remain authoritative when Valkey notifications are lost. Use explicit index lifecycle commands for upgrades and rollback:

```bash
rtk proxy "<skill-root>/scripts/research-db" worker --batch-size 32 --poll-seconds 5 --lease-seconds 300
rtk proxy "<skill-root>/scripts/research-db" index-list
rtk proxy "<skill-root>/scripts/research-db" index-build --current-config --all
rtk proxy "<skill-root>/scripts/research-db" index-activate "<index-id>"
rtk proxy "<skill-root>/scripts/research-db" index-rollback "<index-id>"
rtk proxy "<skill-root>/scripts/research-db" index-prune --dry-run
```

Initialize with `research-db migrate`, then use `research-db ingest-ready` for the writable-store preflight. Persistence `on` runs that preflight before Firecrawl acquisition. Treat `doctor` as read-only: it reports schema, blob, worker, job, active-index, Qdrant coverage, and model-service health without creating or repairing anything. Import old scratch trees with `import-scratch --dry-run` before applying the idempotent import. Use `rederive` to rebuild parser/chunker derivations from retained blob bytes, and `export-invocation fc_<uuid>` to reconstruct `_corpus.json` after an interrupted export.

Read `references/research-store-architecture.md` for boundaries and consistency rules. Read `references/research-store-operations.md` before deploying the worker, changing an embedding definition, migrating, restoring, rebuilding, pruning, or running live fixtures. Read `references/workflow-state-schema.md` for the authoritative workflow tables and `references/budget-policy.md` for deterministic caps, rejection rules, persisted budget snapshots, and v7 repair.

For operations, deployment, backup/restore, Qdrant rebuild, Valkey loss, endpoint restart, interrupted-run recovery, benchmarking, destructive commands, and the complete configuration variable reference, read `references/operations-runbook.md`. For migration procedures, forward-repair, and the full migration sequence, read `references/migration-guide.md`. For coding-agent guidance on architecture, execution modes, budget policy, state machine, retrieval, evidence, synthesis, cache, and resource governance, read `references/coding-agent-guide.md`.

## Scripts

| Script                  | Purpose                                                                                                       | Output files                                                                                                         |
| ----------------------- | ------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `scripts/fsearch_smart` | Coverage-led research orchestrator via `ResearchOrchestrator`                                                 | `fc_<uuid>/smart/` with consolidated metadata and scratch diagnostic files                                        |
| `scripts/fsearch`       | Search Firecrawl, preserve all candidates, and scrape a bounded subset                                        | `_search.json`, `_index.md`, `_context.json`, `_candidates.json`, `_meta.json`, `result_NNN.md` or `result_NNN.json` |
| `scripts/fscrape`       | Scrape arbitrary URLs to scratch files                                                                        | `_index.md`, `_meta.json`, `url_NNN.md` or `url_NNN.json`                                                            |
| `scripts/fread`         | Read scratch files, list history, walk directories, and grep results                                          | Console output only                                                                                                  |
| `scripts/research-db`   | Migrate, import, inspect, retrieve, rederive, index, reconcile, export, and diagnose the authoritative corpus | JSON manifests and bounded passages                                                                                  |

## Procedure

### 1. Apply the Budget Policy

`fsearch_smart` maps a validated `ResearchSpec` to `budget-policy-v1`. If no spec file is supplied, it creates a narrow deterministic fallback that preserves the exact objective as one question and marks unresolved semantics. The coverage-led `ResearchOrchestrator` is the only execution path, and semantic scope—not objective length—selects the policy tier.

| Policy tier | Semantic floor                                                              | Search and extraction caps                   |
| ----------- | --------------------------------------------------------------------------- | -------------------------------------------- |
| `focused`   | Low-risk, narrow semantic scope                                             | 2 x 15 candidates; 8 attempts; 6 successes   |
| `standard`  | Medium risk, freshness, corroboration, or multipart scope                   | 3 x 25 candidates; 18 attempts; 12 successes |
| `intensive` | High risk, expected disagreement, broad source requirements, or large scope | 5 x 40 candidates; 36 attempts; 25 successes |

```bash
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" --research-spec spec.json
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" --research-run-id "$RUN_ID"
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" --dry-run
```

The orchestrator generates a structured research brief and query plan via LLM planning (through `model_gateway`), then executes coverage-led acquisition. If planning degrades to a single exact-objective query, the orchestrator proceeds conservatively. Direct Gemini planner access, keyword-complexity classification, and first-match profile selection are retired (P7-08 / #68).

Treat search, extraction attempts, and successful extractions as separate hard budgets. The orchestrator evaluates budget policy, creates coverage items before acquisition, and evaluates coverage after each meaningful wave. Failed extractions advance to replacement candidates and emit pivot events.

Use a validated `ResearchSpec` to express scope and source requirements. `--max-adaptive-cycles` may tighten the orchestrator's policy-authorized cycle ceiling but cannot exceed it. `_budget.json` records the policy authorization boundary; `_meta.json` records planning provenance and diagnostic strategy metadata.

### 2. Run Single-Query Search When Needed

Use `fsearch` for a specific query or when you already know the query shape.

```bash
rtk proxy "<skill-root>/scripts/fsearch" "<query>" --limit 20 --scrape-limit 5 --sources web,news --tbs qdr:d
rtk proxy "<skill-root>/scripts/fsearch" "<query>" --limit 50 --scrape-limit 0
```

Workflow:

1. Note the scratch directory printed by the command.
2. Record the printed invocation ID when coordinating multiple research steps.
3. Read `_index.md` for orientation, then `_context.json` for the compact selected-source manifest.
4. Use `_candidates.json` only when screening unscripted candidates, domains, branch provenance, or scores.
5. Treat `result_000.md` as rank 1, `result_001.md` as rank 2, and so on.
6. If an unscripted candidate is more relevant, scrape its stored URL into the existing scratch directory; do not repeat the search. For a single query, reuse its raw response with `--scrape-ranks 2,7,11 --dir <dir>`.
7. Use `research-db run-status <run-id>` for workflow state and `fread <scratch-dir>` for mechanical acquisition diagnostics.

### 3. Scrape Known URLs

```bash
rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/article" "https://example.com/article2"
```

Use `--output-dir <dir>` to reuse an existing scratch directory.

For structured extraction, pass an inline JSON schema or a schema file path. Schema mode forces JSON output and skips markdown cleanup.

```bash
rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/product" \
  --schema '{"type":"object","properties":{"name":{"type":"string"},"price":{"type":"string"}},"required":["name","price"]}'

rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/product" --schema-file "./schema.json"
```

### 4. Inspect Scratch Files Selectively

Do not load large scrape files directly. Use `fread` to inspect indexes, grep directories, and read slices.

```bash
rtk proxy "<skill-root>/scripts/fread" "$SCRATCH_DIR"
rtk proxy "<skill-root>/scripts/fread" "$SCRATCH_DIR" --grep "pattern"
rtk proxy "<skill-root>/scripts/fread" "$SCRATCH_DIR/result_001.md" --skip 30 --lines 50
rtk proxy "<skill-root>/scripts/fread" --history
```

## Decision Flow

If zero successful pages are scraped:

- Inspect `research-db run-status <run-id>` and `research-db doctor` first. Then read the branch `_search_error.log`; if candidates were returned but no pages scraped, distinguish candidate acquisition from extraction failure before changing the query.
- Broaden the query only after distinguishing those cases.
- Remove restrictive `--tbs` filters.
- Inspect `_candidates.json` for promising unscripted ranks and reuse `_search.json` before searching again.
- For a direct `fsearch`, increase `--limit` when candidate coverage is thin and `--scrape-limit` only when the candidate ledger already contains strong unscripted sources.
- Try the MCP fallback if the Firecrawl CLI itself is unavailable.

If multiple smart-search branches exist:

- Read the consolidated root index first.
- Read `_context.json` next; it maps the selected sources to their branch, facet, score, file, and word count without loading full pages.
- Use `_candidates.json` to compare all unique URLs only when the selected context is insufficient.
- Use `_meta.json` for complete strategy, query, retry, and per-branch provenance.
- Treat `query_*/_search.json` as the immutable raw acquisition layer; do not rerun a search merely to choose a different candidate.
- Grep the root directory for exact terms.
- Load only the specific result files and line ranges that matter.
- Do not pass a single result file to `fread --grep`; grep its containing scratch directory instead.

## MCP Fallback

The CLI scripts are the primary workflow because they preserve scratch files and metadata. If the `firecrawl` command is unavailable or broken, use available Firecrawl MCP tools directly:

1. Search with the Firecrawl MCP search tool.
2. Scrape the best URLs with the Firecrawl MCP scrape tool, requesting markdown.
3. Write results to scratch files manually if the task still needs disk-backed context control.

## Markdown Cleanup

`fsearch` and `fscrape` automatically request common boilerplate exclusions from the Firecrawl CLI and then run `scripts/cleanup.py` from the skill's local `scripts/` directory. The cleanup pass normalizes whitespace, strips common boilerplate, removes tracking query parameters from markdown links, and simplifies long markdown image references.

## Verification

- Index shows at least one `[OK]` result, or the error count is expected.
- Scratch file exists at the path stated in the index.
- `fread` output starts with a header like `-- fread: result_NNN.md --` followed by file stats.
- Content preview matches the article topic rather than site navigation or anti-bot boilerplate.
- Enabled persistence records the invocation batch and every success/failure, and `_corpus.json` resolves to the same stable IDs.
- The worker reports a current heartbeat, no stale or dead fixture jobs, and exact PostgreSQL/Qdrant coverage for the active fingerprint.
- `doctor` changes no database, blob, Qdrant, Valkey, or filesystem state.

Run the full deterministic suite without network usage:

```bash
rtk proxy env PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider \
  "<skill-root>/scripts/"
```

Run `test_research_store_integration.py` only against an explicit disposable PostgreSQL database whose name contains a standalone `test` segment, with `RESEARCH_STORE_TEST_ALLOW_RESET` set to that exact database name. It permanently covers non-empty multi-index v1-to-v5 migration, concurrent idempotent ingestion, derivation selection, atomic retry ledgers, run/lease immutability, final-attempt expiry, and manifest-definition binding. Separately record an acceptance campaign against disposable services that proves wrapper preflight/fail-closed behavior, Valkey loss tolerance, damaged-index rebuilding, active-alias cutover, and rollback before touching live state.

Run the explicit, operation-capped self-hosted campaign only when live API use is intended:

```bash
rtk proxy "<skill-root>/scripts/live_validate.py" \
  --api-url "${FIRECRAWL_API_URL:-http://localhost:3002}" \
  --max-operations 125
```

Inspect the generated `report.md` and `manifest.json` below the printed platform-temporary artifact directory. Never treat backend reachability failures as query-quality failures.

For an authorized live-corpus campaign, retain a tagged `research-store-v3` fixture set with unchanged and changed snapshots, overlapping positive controls, an unrelated negative control, one `fscrape`, one bounded `fsearch`, and one bounded parent `smart_search`. Verify wrapper-to-batch-to-PostgreSQL/blob-to-worker-to-Qdrant-to-hybrid-retrieval provenance, worker restart recovery, index activation and rollback, linked `fr_<uuid>` retrieval events, and private mode producing no PostgreSQL, blob, or Qdrant writes.
