---
name: firecrawl
description: "Search and scrape web content with Firecrawl scratch files. Use when Codex needs to search the web, scrape multiple URLs, preserve large results on disk, inspect metadata indexes, grep or selectively read scraped pages, or keep web research context small."
---

# Firecrawl Scratch-File Workflow

Use the bundled Firecrawl CLI wrappers to write search and scrape output to scratch files, generate compact indexes, and load only the files or excerpts needed for the task. Prefer this skill for research that benefits from preserved source provenance or later audit; use direct MCP tools only when the CLI workflow is unavailable.

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

## Persistent Invocation Catalog

In addition to temporary scratch artifacts, every top-level invocation writes a durable audit record below `${XDG_DATA_HOME:-~/.local/share}/firecrawl/`. Catalog v5 retains the initial query, structured research brief, generated strategy, branch telemetry, mechanical operation facts, bounded excerpts, date signals, diagnostic errors, and artifact hashes. It does not copy full scraped page bodies or secrets.

Use `FIRECRAWL_CATALOG_DIR` to choose a different catalog root, or `FIRECRAWL_CATALOG_DISABLED=1` for private or isolated runs. Smart searches create one parent record with ordered child events rather than duplicate records for internal branches.

For multi-step research, create one explicit `fr_<uuid>` research run and pass it to every top-level search or scrape. Finish it with a source manifest that maps claims to the exact candidates and excerpts actually used, plus the delivered answer when available. `--used-url` remains a lower-fidelity convenience.

```bash
RUN_ID="$(rtk proxy \"<skill-root>/scripts/frun\" start \"<research objective>\" --profile auto)"
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" --research-run-id "$RUN_ID"
rtk proxy "<skill-root>/scripts/fscrape" "https://primary.example" --research-run-id "$RUN_ID"
rtk proxy "<skill-root>/scripts/frun" finish "$RUN_ID" --outcome satisfied --source-manifest sources.json --answer-file final.md
rtk proxy "<skill-root>/scripts/fread" --catalog "$RUN_ID"
```

```bash
rtk proxy "<skill-root>/scripts/fread" --catalog
rtk proxy "<skill-root>/scripts/fread" --catalog fc_<uuid>
rtk proxy "<skill-root>/scripts/fread" --catalog fr_<uuid>
```

For a failed, empty, or unexpectedly weak run, inspect this catalog record before retrying. Catalog v5 separates mechanical execution and data completeness from LLM-assessed strategy, acquisition, evidence, freshness, authority, claim support, and outcome consistency. Programmatic code never assigns semantic quality labels. Use `--json` only when the concise audit summary is insufficient.

Finished runs are immutable. Reopen one explicitly before attaching more work, and annotate material pivots so later audits do not have to infer the reason from chronology alone:

```bash
rtk proxy "<skill-root>/scripts/frun" reopen "$RUN_ID" --reason "add missing official corroboration"
rtk proxy "<skill-root>/scripts/frun" annotate "$RUN_ID" --type pivot --reason "generic search results; switched to direct official URLs"
rtk proxy "<skill-root>/scripts/frun" verify "$RUN_ID"
```

Every completed explicit research run receives a staged local LLM audit by default; set `FIRECRAWL_AUDIT_AUTO_SEMANTIC=0` to disable it. The audit builds an objective-specific rubric, assesses acquisition and evidence separately, then synthesizes cited findings. Commercial providers are never called without explicit selection:

```bash
rtk proxy "<skill-root>/scripts/frun" audit "$RUN_ID" --llm local
rtk proxy "<skill-root>/scripts/frun" audit "$RUN_ID" --auto-semantic
rtk proxy "<skill-root>/scripts/frun" audit "$RUN_ID" --llm openai --model "<model-id>"
rtk proxy "<skill-root>/scripts/frun" audit-status "$RUN_ID"
rtk proxy "<skill-root>/scripts/frun" aggregate
rtk proxy "<skill-root>/scripts/frun" recompute "$RUN_ID"
rtk proxy "<skill-root>/scripts/frun" compare "$RUN_ID" "$OTHER_RUN_ID"
```

Read `references/catalog-v5.md` when constructing a source manifest, interpreting audit stages, resetting the catalog after a schema change, or configuring an LLM provider and context budget.

Purge stale audit data only when it is no longer useful for comparison. Purge is always a dry run without `--force`; use `--before`, `--run-id`, `--keep-last`, or `--orphans` for bounded cleanup. With no filter, `--force` removes the entire resolved catalog root.

```bash
rtk proxy "<skill-root>/scripts/frun" purge
rtk proxy "<skill-root>/scripts/frun" purge --keep-last 10
rtk proxy "<skill-root>/scripts/frun" migrate --from 4 --to 5
rtk proxy "<skill-root>/scripts/frun" migrate --from 4 --to 5 --apply
rtk proxy "<skill-root>/scripts/frun" purge --force
```

Treat a schema transition as a clean audit boundary. `migrate` is a dry run by default; `--apply` discards the entire prior catalog, including records, snapshots, assessments, events, and migration artifacts, then initializes an empty catalog at the new schema. Never convert or back up old audit records during a schema transition.

See `references/cli-script-disambiguation.md` when the `firecrawl` command is missing or when you need to distinguish the Node.js CLI, Python SDK, and MCP tools.

## Authoritative Research Asset Store

When `DATABASE_URL` is configured, every successful `fsearch` and `fscrape` result is persisted through the research-store service before the wrapper completes. PostgreSQL is authoritative; content-addressed blobs retain immutable payload bytes; Qdrant is a rebuildable retrieval projection; Valkey is transient coordination only. The wrapper also writes `_corpus.json` with stable source, snapshot, document, and chunk IDs. Scratch files remain compatibility/debugging exports and must not be scanned for routine retrieval.

Use manifest-first database operations for retained research:

```bash
rtk proxy "<skill-root>/scripts/research-db" corpus-overview
rtk proxy "<skill-root>/scripts/research-db" search-assets "<query>" --limit 20
rtk proxy "<skill-root>/scripts/research-db" inspect-asset "<candidate-id>"
rtk proxy "<skill-root>/scripts/research-db" fetch-passages "<candidate-id>" --max-tokens 2000
```

`search-assets` uses PostgreSQL full-text candidates plus Qdrant dense candidates, reciprocal-rank fusion, and the configured local reranker over a bounded fused set. Candidate manifests expose lexical, semantic, fused, and reranker scores without preloading full documents.

Initialize and diagnose the store with `research-db migrate` and `research-db doctor`. Import old scratch trees with `import-scratch --dry-run` before applying the idempotent import. Read `references/research-store-architecture.md` for boundaries and `references/research-store-operations.md` for deployment, backup/restore, rebuild, recovery, and configuration.

## Scripts

| Script | Purpose | Output files |
| --- | --- | --- |
| `scripts/fsearch_smart` | Recommended LLM-planned, metadata-first, adaptively scraped research orchestrator | `fc_<uuid>/smart/` with branch directories, consolidated metadata, and `_evidence.json` |
| `scripts/fsearch` | Search Firecrawl, preserve all candidates, and scrape a bounded subset | `_search.json`, `_index.md`, `_context.json`, `_candidates.json`, `_meta.json`, `result_NNN.md` or `result_NNN.json` |
| `scripts/fscrape` | Scrape arbitrary URLs to scratch files | `_index.md`, `_meta.json`, `url_NNN.md` or `url_NNN.json` |
| `scripts/fread` | Read scratch files, list history, walk directories, and grep results | Console output only |
| `scripts/research-db` | Migrate, import, inspect, retrieve, reindex, reconcile, export, and diagnose the authoritative corpus | JSON manifests and bounded passages |

## Procedure

### 1. Choose Search Depth

Before searching, classify the request's complexity. Let `fsearch_smart` auto-classify unless the user has made scope clear.

| Complexity | Use for | Strategy |
| --- | --- | --- |
| `simple` | Fact checks, direct setup lookups, single definitions | Acquire 2 x 15 candidates, then scrape 6 globally selected pages |
| `moderate` | Comparisons, configuration options, API usage, multi-step setup | Acquire 3 x 25 candidates, then scrape 12 globally selected pages |
| `complex` | Deep research, troubleshooting, academic topics, architecture choices | Acquire 5 x 40 candidates, then scrape 25 globally selected pages |

```bash
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" --complexity moderate
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" --complexity complex --tbs qdr:w
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" --complexity complex --planner heuristic --dry-run
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" --complexity moderate --results-per-query 40 --total-scrapes 18
```

Use the default `auto` planner with the local model. It first produces a structured research brief and then a complementary query plan. If planning fails, the workflow runs only the exact objective as a degraded query; it does not silently broaden through deterministic facets. Use `--planner heuristic` only for reproducible legacy diagnostics. Commercial planning or fallback requires explicit provider and model options.

Treat search and scrape as separate budgets. `fsearch_smart` issues each search once, saves every raw response, deduplicates candidates, asks the selected LLM to triage compact candidate cards, and scrapes in bounded waves. Failed extractions advance to replacement candidates and emit pivot events. Increase `--results-per-query` before `--total-scrapes`; metadata remains the inexpensive orientation layer.

Use `--max-searches`, `--max-scrapes`, and `--max-iterations` as hard limits. Use `--no-llm-triage` only to diagnose model-independent acquisition. Read `_evidence.json` after `_context.json`; it contains the research brief, candidate decisions, scrape-wave provenance, source dossiers, coverage placeholders, and limitations for answer composition.

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
7. Use `fread --catalog <invocation-id>` when comparing this run with an earlier run or diagnosing its mechanical acquisition and extraction metrics.

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

- Inspect `fread --catalog <invocation-id>` first. If acquisition failed, read the branch `_search_error.log`; if candidates were returned but no pages scraped, distinguish candidate acquisition from extraction failure before changing the query.
- Broaden the query only after distinguishing those cases.
- Remove restrictive `--tbs` filters.
- Inspect `_candidates.json` for promising unscripted ranks and reuse `_search.json` before searching again.
- Increase `--results-per-query` when candidate coverage is thin; increase `--total-scrapes` only when the ledger already contains strong unscripted sources.
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

Run deterministic validation without network usage:

```bash
rtk proxy env PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider \
  "<skill-root>/scripts/test_classifier.py" "<skill-root>/scripts/test_workflow.py"
```

Run the explicit, operation-capped self-hosted campaign only when live API use is intended:

```bash
rtk proxy "<skill-root>/scripts/live_validate.py" \
  --api-url "${FIRECRAWL_API_URL:-http://localhost:3002}" \
  --max-operations 125 --planner both
```

Inspect the generated `report.md` and `manifest.json` below the printed platform-temporary artifact directory. Never treat backend reachability failures as query-quality failures.
