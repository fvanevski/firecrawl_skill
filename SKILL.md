---
name: firecrawl
description: "Acquire, retain, retrieve, and audit web research with Firecrawl. Use when Codex needs to search or scrape the web, query or inspect the authoritative PostgreSQL research corpus, run hybrid lexical/vector retrieval, fetch bounded citation passages, diagnose ingestion or indexing, or manage research provenance and recovery."
---

<!-- @format -->

# Firecrawl Research Corpus and Acquisition

PostgreSQL is the sole authority for research workflow state, invocations, claims, audits, and corpus identities. Content-addressed blobs retain immutable payloads, Qdrant is a rebuildable vector projection, Valkey provides transient wakeups, and local temporary files are never authoritative storage. Use the database first for retained research. Use Firecrawl acquisition wrappers when the corpus lacks current evidence, then retrieve through compact database manifests and bounded passages.

## Choose the First Operation

1. Run `research-db corpus-overview` and `search-assets "<query>"` for retained-corpus questions.
2. Inspect promising candidate manifests, then call `fetch-passages` with a token bound. Do not preload full documents.
3. Run `fsearch_smart`, `fsearch`, or `fscrape` only when the corpus is empty, stale, incomplete, or the request explicitly requires new web acquisition.
4. Inspect the invocation or research-run record before retrying failed or weak acquisition. Distinguish acquisition, persistence, indexing, and retrieval failures.
5. Use `finspect` and bounded `research-db` inspection commands for history, replay, asset inspection, and passages.

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
rtk proxy "<skill-root>/scripts/finspect" runs
```

Keep `rtk proxy` at the outer agent-visible boundary. Do not add RTK inside the wrappers: their direct `firecrawl` subprocesses must retain unmodified streams and exit codes.

`fsearch` and `fscrape` require a valid `fr_<uuid>` run and a writable PostgreSQL store before provider execution. They return bounded JSON or console results containing stable run, invocation, response, candidate, snapshot, document, chunk, batch, and index-job identities. Payload bytes are retained only in the immutable content-addressed blob store.

Generate one `fc_<uuid>` invocation ID for every top-level wrapper run. Use `--invocation-id` only to deliberately attach a retry to an existing invocation; use the same idempotency key for uncertain retries.

## PostgreSQL Workflow Provenance

Every persistent top-level operation belongs to an explicit `fr_<uuid>` run and records an authoritative `fc_<uuid>` invocation in PostgreSQL. Wrapper startup validates the run before network acquisition, records the invocation, and advances only permitted lifecycle stages. Wrapper completion records the terminal invocation status and advances to indexing only after corpus persistence succeeds. No filesystem manifest is read to determine run, invocation, replay, or corpus state.

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

PostgreSQL is authoritative; content-addressed blobs retain immutable payload bytes; versioned Qdrant collections are rebuildable retrieval projections; Valkey provides optional wakeups only. Every supported acquisition command requires authoritative preflight and fails before Firecrawl execution when PostgreSQL, schema, blob durability, or run binding is invalid. There is no non-persistent acquisition success path.

Use database operations for retained research:

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

Initialize with `research-db migrate`, then use `research-db ingest-ready` for the writable-store preflight. Treat `doctor` as read-only: it reports schema, blob, worker, job, active-index, Qdrant coverage, and model-service health without creating or repairing anything. Old filesystem trees must be migrated with the last pre-removal release or an external one-shot migration tool. Use `rederive` to rebuild parser/chunker derivations from retained blob bytes, and explicit export commands to reconstruct requested JSON artifacts from authoritative records.

Read `references/research-store-architecture.md` for boundaries and consistency rules. Read `references/research-store-operations.md` before deploying the worker, changing an embedding definition, migrating, restoring, rebuilding, pruning, or running live fixtures. Read `references/workflow-state-schema.md` for the authoritative workflow tables and `references/budget-policy.md` for deterministic caps, rejection rules, persisted budget snapshots, and v7 repair.

For operations, deployment, backup/restore, Qdrant rebuild, Valkey loss, endpoint restart, interrupted-run recovery, benchmarking, destructive commands, and the complete configuration variable reference, read `references/operations-runbook.md`. For migration procedures, forward-repair, and the full migration sequence, read `references/migration-guide.md`. For coding-agent guidance on architecture, execution modes, budget policy, state machine, retrieval, evidence, synthesis, cache, and resource governance, read `references/coding-agent-guide.md`.

## Scripts

| Script | Purpose | Authoritative result |
| --- | --- | --- |
| `scripts/fsearch_smart` | Coverage-led research orchestrator via `ResearchOrchestrator` | PostgreSQL workflow, acquisition, provenance, and corpus records |
| `scripts/fsearch` | Search Firecrawl, persist all candidates, and scrape a bounded subset | Stable response, candidate, extraction, corpus, batch, and job IDs |
| `scripts/fscrape` | Scrape arbitrary URLs through the direct authoritative service | Stable snapshot, document, derivation, chunk, batch, and job IDs |
| `scripts/finspect` | List, replay, inspect, and retrieve bounded retained records | Bounded JSON/console output |
| `scripts/research-db` | Migrate, inspect, retrieve, rederive, index, reconcile, export, and diagnose the authoritative corpus | JSON manifests and bounded passages |

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
rtk proxy "<skill-root>/scripts/fsearch" "<query>" --research-run-id "$RUN_ID" --limit 20 --scrape-limit 5 --sources web,news --tbs qdr:d
rtk proxy "<skill-root>/scripts/fsearch" "<query>" --research-run-id "$RUN_ID" --limit 50 --scrape-limit 0 --json
```

The command commits the search response and all candidates before reporting success. Selected extraction uses stable candidate IDs internally. Inspect or replay retained search results with `finspect search-responses`, `finspect replay-search`, and `finspect scrape-candidates`; do not rerun a query merely to select different candidates.

### 3. Scrape Known URLs

```bash
rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/article" "https://example.com/article2" --research-run-id "$RUN_ID"
```

For structured extraction, pass an inline JSON schema or a schema file path. Schema mode forces JSON output and persists the validated structured payload with its MIME type and provenance.

```bash
rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/product" --research-run-id "$RUN_ID" \
  --schema '{"type":"object","properties":{"name":{"type":"string"},"price":{"type":"string"}},"required":["name","price"]}'

rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/product" --research-run-id "$RUN_ID" --schema-file "./schema.json"
```

### 4. Inspect Retained Results Selectively

Use database-native commands rather than loading full payloads.

```bash
rtk proxy "<skill-root>/scripts/finspect" runs
rtk proxy "<skill-root>/scripts/finspect" invocations --run-id "$RUN_ID"
rtk proxy "<skill-root>/scripts/finspect" search-responses --run-id "$RUN_ID"
rtk proxy "<skill-root>/scripts/finspect" inspect "<asset-id>"
rtk proxy "<skill-root>/scripts/finspect" passages "<asset-id>" --max-tokens 2000
```

## Decision Flow

If zero successful pages are acquired:

- Inspect `research-db run-status <run-id>`, the invocation record, extraction attempts, and `research-db doctor` before changing the query.
- Distinguish search transport, candidate parsing, extraction, ingestion, and indexing failures.
- Broaden the query only after identifying the failed stage.
- Remove restrictive `--tbs` filters when candidate coverage is thin.
- Increase `--limit` when the retained candidate ledger is weak and `--scrape-limit` only when it already contains strong candidates.
- Replay the stored response and select candidates by stable ID rather than repeating acquisition.

For smart-search branches, use authoritative workflow and provenance records for normal execution and resume. Local diagnostics remain non-authoritative until their removal under #190.

## MCP Fallback

The bundled CLI scripts are the primary workflow because they enforce authoritative preflight and persistence. If the `firecrawl` command is unavailable or broken, use available Firecrawl MCP tools only through an integration that persists the response into the authoritative acquisition service; do not treat ad hoc local files as successful acquisition.

## Markdown Cleanup

`fsearch` and `fscrape` automatically request common boilerplate exclusions from the Firecrawl CLI and then run `scripts/cleanup.py` from the skill's local `scripts/` directory. The cleanup pass normalizes whitespace, strips common boilerplate, removes tracking query parameters from markdown links, and simplifies long markdown image references.

## Verification

- The command reports stable authoritative IDs or a stage-specific failure.
- Search response and candidates are committed before success is reported.
- Scrape payload bytes resolve through `BLOB_ROOT`, and PostgreSQL provenance resolves to the same snapshot, document, derivation, and chunk IDs.
- Bounded passages match the source topic rather than site navigation or anti-bot boilerplate.
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

For an authorized live-corpus campaign, retain a tagged `research-store-v3` fixture set with unchanged and changed snapshots, overlapping positive controls, an unrelated negative control, one `fscrape`, one bounded `fsearch`, and one bounded parent `smart_search`. Verify wrapper-to-batch-to-PostgreSQL/blob-to-worker-to-Qdrant-to-hybrid-retrieval provenance, worker restart recovery, index activation and rollback, linked `fr_<uuid>` retrieval events, and failed authoritative preflight producing no Firecrawl, PostgreSQL, blob, or Qdrant writes.
