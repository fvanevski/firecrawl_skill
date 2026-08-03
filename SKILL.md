---
name: firecrawl
description: "Acquire, retain, retrieve, and audit web research with Firecrawl. Use when Codex needs to search or scrape the web, inspect the PostgreSQL-authoritative corpus, replay retained responses, select stable candidates, retrieve bounded passages, diagnose ingestion or indexing, or recover research provenance."
---

<!-- @format -->

# Firecrawl Research Corpus and Acquisition

PostgreSQL is authoritative for workflow, acquisition provenance, corpus identities, and durable jobs. `BLOB_ROOT` retains immutable payload bytes. Qdrant is a rebuildable projection, and Valkey is optional transient coordination. This is the Target A boundary: payload bytes remain outside PostgreSQL.

## Choose the first operation

1. Search retained material first with `corpus-overview`, `search-assets`, and bounded `fetch-passages`.
2. Use `finspect` for run history, retained-response replay, candidate selection, attempts, and bounded inspection.
3. Acquire new evidence with `fsearch_smart`, `fsearch`, or `fscrape` only when retained evidence is absent, stale, incomplete, or the task explicitly requires current acquisition.
4. Inspect the authoritative run and invocation before retrying. Distinguish provider, parsing, ingestion, indexing, and retrieval failures.
5. Never infer success or current state from a local path, presentation export, Qdrant point, or Valkey message.

Resolve `<skill-root>` to the directory containing this file and keep `rtk proxy` at the outer agent-visible boundary.

```bash
rtk proxy "<skill-root>/scripts/research-db" corpus-overview
rtk proxy "<skill-root>/scripts/research-db" search-assets "<query>" --limit 20
rtk proxy "<skill-root>/scripts/research-db" inspect-asset "<candidate-id>"
rtk proxy "<skill-root>/scripts/research-db" fetch-passages "<candidate-id>" --max-tokens 2000
```

## Authoritative acquisition

`fsearch` and `fscrape` require `DATABASE_URL`, an Alembic-head writable PostgreSQL store, a durable writable `BLOB_ROOT`, and a valid acquisition-eligible `fr_<uuid>`. Their preflight completes before Firecrawl construction or network execution. A failed preflight cannot become a successful non-persistent acquisition.

```bash
RUN_ID="$(rtk proxy "<skill-root>/scripts/frun" start "<research objective>" --mode autonomous_local)"

rtk proxy "<skill-root>/scripts/fsearch" "<query>" \
  --research-run-id "$RUN_ID" \
  --limit 20 \
  --scrape-limit 5 \
  --sources web,news

rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/article" \
  --research-run-id "$RUN_ID"

rtk proxy "<skill-root>/scripts/research-db" run-status "$RUN_ID"
rtk proxy "<skill-root>/scripts/research-db" doctor
```

A normal top-level operation receives a new `fc_<uuid>`. Use `--invocation-id` and the same idempotency key only for a deliberate retry of uncertain identical input. Conflicting key reuse fails closed.

`fsearch_smart` creates an authoritative run when one is not supplied. `--dry-run` is the only non-persistent execution surface and performs no database or network writes.

```bash
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>"
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" --research-run-id "$RUN_ID"
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" --dry-run
```

## Stable replay and candidate selection

Use database-native identities, not local filenames or ranks.

```bash
rtk proxy "<skill-root>/scripts/finspect" runs --limit 20
rtk proxy "<skill-root>/scripts/finspect" invocations --run "$RUN_ID" --limit 20
rtk proxy "<skill-root>/scripts/finspect" search-responses --run "$RUN_ID" --limit 20
rtk proxy "<skill-root>/scripts/finspect" replay-search "<search-response-uuid>"
rtk proxy "<skill-root>/scripts/finspect" scrape-candidates "<candidate-uuid>" \
  --format markdown \
  --idempotency-key "<stable-key>"
rtk proxy "<skill-root>/scripts/finspect" retry-candidates "<prior-invocation-uuid>" \
  --idempotency-key "<new-stable-key>"
rtk proxy "<skill-root>/scripts/finspect" attempts --run "$RUN_ID"
rtk proxy "<skill-root>/scripts/finspect" inspect "<asset-uuid>"
rtk proxy "<skill-root>/scripts/finspect" passages "<asset-uuid>" \
  --limit 20 \
  --max-chars 20000 \
  --max-tokens 4000
```

`replay-search` verifies retained byte length and SHA-256 before returning the payload and never calls Firecrawl. `scrape-candidates` and `retry-candidates` perform the same authoritative preflight as direct acquisition.

## Structured scrape

```bash
rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/product" \
  --research-run-id "$RUN_ID" \
  --schema '{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}' \
  --json
```

Structured provider output is validated before successful document ingestion. Invalid structured output remains a failed extraction attempt; it is not silently accepted.

## Workflow and completion

The acquisition path advances only through permitted PostgreSQL transitions:

```text
created → planning → corpus_review → acquiring → extracting → indexing
```

After all run-scoped index jobs complete, `frun finish` advances through coverage review, synthesis, validation, and the requested terminal outcome.

```bash
rtk proxy "<skill-root>/scripts/frun" finish "$RUN_ID" --outcome satisfied
rtk proxy "<skill-root>/scripts/frun" status "$RUN_ID"
```

Retry an uncertain command with its original idempotency key. After a stale revision, inspect `run-status` before deciding whether a new command is valid. Reopen terminal runs explicitly.

## Qdrant and Valkey recovery

PostgreSQL jobs remain durable when Valkey is unavailable. Qdrant contains no unique workflow or corpus truth.

```bash
rtk proxy "<skill-root>/scripts/research-db" index-list
rtk proxy "<skill-root>/scripts/research-db" index-build --current-config --all
rtk proxy "<skill-root>/scripts/research-db" worker --once --batch-size 64
rtk proxy "<skill-root>/scripts/research-db" reconcile-qdrant
rtk proxy "<skill-root>/scripts/research-db" index-activate "<index-id>"
rtk proxy "<skill-root>/scripts/research-db" index-rollback "<prior-index-id>"
rtk proxy "<skill-root>/scripts/research-db" doctor
```

Never embed a query against an alias backed by a different embedding fingerprint. Retrieval falls back to PostgreSQL lexical search and `doctor` reports the mismatch.

## Explicit exports

```bash
rtk proxy "<skill-root>/scripts/research-db" export-invocation "fc_<uuid>" --output invocation.json
rtk proxy "<skill-root>/scripts/research-db" export-run "$RUN_ID" --output run.json
```

Exports are never replay, retry, selection, ingestion, or workflow inputs.

## Documentation

- `references/research-store-architecture.md`: Target A authority and consistency.
- `references/operations-runbook.md`: deployment, backup, restore, worker, projection, and recovery.
- `references/research-store-operations.md`: compact operator commands.
- `references/migration-guide.md`: schema and legacy-tree migration.
- `references/recovery-drill-checklist.md`: executable recovery drills.
- `references/cli-script-disambiguation.md`: Node CLI, Python SDK, and MCP boundaries.
- `references/coding-agent-guide.md`: implementation and testing constraints.
- `references/workflow-state-schema.md`: lifecycle and invocation contracts.
- `references/release-notes-rc9.md`: breaking changes and rollback boundary.

## Verification

- A supported acquisition reports stable authoritative IDs or a stage-specific failure.
- No Firecrawl or network invocation occurs after failed authoritative preflight.
- PostgreSQL identities resolve to immutable bytes under `BLOB_ROOT`.
- The worker and active Qdrant alias match the configured embedding fingerprint.
- Valkey loss does not strand durable work.
- Bounded inspection observes record, character, byte, and tokenizer limits.
- `doctor` is read-only.

```bash
rtk proxy env PYTHONDONTWRITEBYTECODE=1 \
  pytest -q -p no:cacheprovider "<skill-root>/scripts/"
```
