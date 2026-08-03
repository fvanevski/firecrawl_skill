---
name: firecrawl
description: "Acquire, retain, retrieve, and audit web research with Firecrawl. Use when Codex needs to search or scrape the web, inspect the PostgreSQL-authoritative corpus, replay retained responses, select stable candidates, retrieve bounded passages, diagnose ingestion or indexing, verify or audit research runs, or recover research provenance."
---

<!-- @format -->

# Firecrawl Research Corpus and Acquisition

PostgreSQL is authoritative for workflow, acquisition provenance, corpus identities, and durable jobs. `BLOB_ROOT` retains immutable payload bytes. Qdrant is a rebuildable projection, and Valkey is optional transient coordination. This is the Target A boundary: payload bytes remain outside PostgreSQL.

## Choose the first operation

1. Search retained material first with `corpus-overview`, `search-assets`, and bounded `fetch-passages`.
2. Use `finspect` for run history, retained-response replay, candidate selection, attempts, exact lexical or pattern search, and bounded inspection.
3. Acquire new evidence with `fsearch_smart`, `fsearch`, or `fscrape` only when retained evidence is absent, stale, incomplete, or the task explicitly requires current acquisition.
4. Inspect the authoritative run and invocation before retrying. Distinguish provider, parsing, ingestion, indexing, retrieval, verification, and audit failures.
5. Never infer success or current state from a local path, presentation export, Qdrant point, or Valkey message.

Resolve `<skill-root>` to the directory containing this file and keep `rtk proxy` at the outer agent-visible boundary. The shell entry points automatically source `scripts/research-env` when it is readable unless `FIRECRAWL_RESEARCH_AUTO_ENV=0` is set deliberately.

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

rtk proxy python3 "<skill-root>/scripts/drain_index_jobs.py" --batch-size 64
rtk proxy "<skill-root>/scripts/research-db" run-status "$RUN_ID"
```

A normal top-level operation receives a new `fc_<uuid>`. Use `--invocation-id` and the same idempotency key only for a deliberate retry of uncertain identical input. Conflicting key reuse fails closed.

`research-db worker --once` processes at most one bounded batch. `drain_index_jobs.py` repeats bounded batches until PostgreSQL reports `claimed=0` and returns nonzero for invalid output, worker failure, failed jobs, lease loss, or an exceeded bound. Do not start another acquisition on the same run while indexing is unfinished.

To add a direct scrape to the same run, first drain and verify the prior work, then drain again after the scrape:

```bash
rtk proxy python3 "<skill-root>/scripts/drain_index_jobs.py" --batch-size 64
rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/article" \
  --research-run-id "$RUN_ID"
rtk proxy python3 "<skill-root>/scripts/drain_index_jobs.py" --batch-size 64
rtk proxy "<skill-root>/scripts/research-db" run-status "$RUN_ID"
```

`fsearch_smart` creates an authoritative run when one is not supplied. `--dry-run` is the only non-persistent execution surface and performs no database or network writes.

```bash
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>"
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" --research-run-id "$RUN_ID"
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" --dry-run
```

A `fsearch_smart` exit status of `75` means the authoritative workflow reached a resumable checkpoint; it is not a generic failure. Preserve the printed `Run ID`, inspect its PostgreSQL state, and resume the same objective with that run rather than creating a replacement run.

```bash
set +e
SMART_OUTPUT="$(
  rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" 2>&1
)"
SMART_STATUS=$?
set -e

printf '%s\n' "$SMART_OUTPUT"
RUN_ID="$(sed -n 's/^Run ID: //p' <<<"$SMART_OUTPUT" | tail -n 1)"

if [[ "$SMART_STATUS" -eq 75 ]]; then
  test -n "$RUN_ID"
  rtk proxy "<skill-root>/scripts/research-db" run-status "$RUN_ID"
  rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" \
    --research-run-id "$RUN_ID"
elif [[ "$SMART_STATUS" -ne 0 ]]; then
  exit "$SMART_STATUS"
fi
```

Planning, budget, provenance, semantic artifacts, and resume checkpoints remain PostgreSQL records. Do not reconstruct a checkpoint from console output or a local file.

## Stable replay, exact search, and candidate selection

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

Use bounded PostgreSQL-native search when an agent needs exact retained text, identifiers, or implementation markers:

```bash
rtk proxy "<skill-root>/scripts/finspect" lexical-search "<terms>" \
  --run "$RUN_ID" \
  --limit 20 \
  --max-chars 20000 \
  --max-tokens 4000

rtk proxy "<skill-root>/scripts/finspect" pattern-search "<literal-or-regex>" \
  --mode literal \
  --run "$RUN_ID" \
  --limit 20 \
  --max-chars 20000 \
  --max-tokens 4000
```

Use `--mode regex` only when regular-expression behavior is required. All inspection output remains bounded and cursor-scoped.

`replay-search` verifies retained byte length and SHA-256 before returning the payload and never calls Firecrawl. `scrape-candidates` and `retry-candidates` perform the same authoritative preflight as direct acquisition.

## Structured scrape

Start a separate run or use the same-run drain boundary above.

```bash
SCRAPE_RUN_ID="$(rtk proxy "<skill-root>/scripts/frun" start "structured scrape" --mode autonomous_local)"
rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/product" \
  --research-run-id "$SCRAPE_RUN_ID" \
  --schema '{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}' \
  --json
rtk proxy python3 "<skill-root>/scripts/drain_index_jobs.py" --batch-size 64
```

Structured provider output is validated before successful document ingestion. Invalid structured output remains a failed extraction attempt; it is not silently accepted.

## Workflow and completion

The following is a common acquisition path, not the complete transition graph:

```text
created → planning → corpus_review → acquiring → extracting → indexing
```

The authoritative PostgreSQL state machine permits these transitions:

```text
created → planning
planning → corpus_review | failed
corpus_review → acquiring | retrieving | failed
acquiring → extracting | coverage_review | partial | failed
extracting → indexing | coverage_review | failed
indexing → coverage_review | partial | failed
coverage_review → acquiring | extracting | retrieving | synthesizing | partial | failed
retrieving → coverage_review | synthesizing | failed
synthesizing → validating | failed
validating → completed | partial | failed
```

Explicit cancellation is available from nonterminal states. `completed`, `partial`, `failed`, and `cancelled` are terminal; further acquisition requires an explicit reopen. Consult `references/workflow-state-schema.md` rather than inferring a legal transition from the abbreviated common path.

After all run-scoped index jobs complete, `frun finish` advances through coverage review, synthesis, validation, and the requested terminal outcome.

```bash
rtk proxy python3 "<skill-root>/scripts/drain_index_jobs.py" --batch-size 64
rtk proxy "<skill-root>/scripts/research-db" run-status "$RUN_ID"
rtk proxy "<skill-root>/scripts/frun" finish "$RUN_ID" --outcome satisfied
rtk proxy "<skill-root>/scripts/frun" status "$RUN_ID"
```

Retry an uncertain command with its original idempotency key. After a stale revision, inspect `run-status` before deciding whether a new command is valid. Reopen terminal runs explicitly.

## Verification, audit, and comparison

Use the run lifecycle wrapper for authoritative completion verification, persisted audits, audit status, and cross-run comparison:

```bash
rtk proxy "<skill-root>/scripts/frun" verify "$RUN_ID"
rtk proxy "<skill-root>/scripts/frun" audit "$RUN_ID"
rtk proxy "<skill-root>/scripts/frun" audit-status "$RUN_ID"
rtk proxy "<skill-root>/scripts/frun" compare "$RUN_ID" "<other-run-id>"
```

`frun verify` checks committed run evidence. `frun audit` persists an audit through the configured semantic authority and deterministic validation path; it is not a substitute for verification. Inspect a nonzero result and the authoritative audit status before retrying. Never print resolved model or provider secrets.

## Qdrant and Valkey recovery

PostgreSQL jobs remain durable when Valkey is unavailable. Qdrant contains no unique workflow or corpus truth.

```bash
rtk proxy "<skill-root>/scripts/research-db" index-list
rtk proxy "<skill-root>/scripts/research-db" index-build --current-config --all
rtk proxy python3 "<skill-root>/scripts/drain_index_jobs.py" --batch-size 64
rtk proxy "<skill-root>/scripts/research-db" reconcile-qdrant
rtk proxy "<skill-root>/scripts/research-db" doctor
rtk proxy "<skill-root>/scripts/research-db" index-activate "<index-id>"
rtk proxy "<skill-root>/scripts/research-db" index-rollback "<prior-index-id>"
```

Never embed a query against an alias backed by a different embedding fingerprint. Retrieval falls back to PostgreSQL lexical search and `doctor` reports the mismatch.

## Explicit exports

```bash
rtk proxy "<skill-root>/scripts/research-db" export-invocation "fc_<uuid>" --output invocation.json
rtk proxy "<skill-root>/scripts/research-db" export-run "$RUN_ID" --output run.json
```

Exports are never replay, retry, selection, ingestion, or workflow inputs.

## Documentation

- `references/authoritative-workflows.md`: canonical acquisition, completion, transaction, and projection-recovery sequences.
- `references/research-store-architecture.md`: Target A authority and consistency.
- `references/operations-runbook.md`: deployment, backup, restore, worker, projection, and recovery.
- `references/research-store-operations.md`: compact operator commands.
- `references/migration-guide.md`: schema and legacy-tree migration.
- `references/recovery-drill-checklist.md`: executable recovery drills.
- `references/cli-script-disambiguation.md`: Node CLI, Python SDK, and MCP boundaries.
- `references/coding-agent-guide.md`: implementation and testing constraints.
- `references/workflow-state-schema.md`: complete lifecycle and invocation contracts.
- `references/release-notes-rc9.md`: breaking runtime and legacy-migration compatibility boundary retained by RC-10.
- `references/release-candidate-gate-rc10.md`: aggregate exact-head gate and mandatory post-merge campaign boundary.
- `references/release-campaign-timing-diagnostics.md`: strict PostgreSQL-bound timing-evidence contract used by the credentialed release campaign.

RC-9 remains the controlling breaking-change and migration boundary. RC-10 adds aggregate and credentialed release validation without introducing a schema migration, command-surface change, payload relocation, Qdrant authority, or Valkey correctness dependency.

## Verification

A supported acquisition reports stable authoritative IDs or a stage-specific failure. In addition:

- No Firecrawl or network invocation occurs after failed authoritative preflight.
- PostgreSQL identities resolve to immutable bytes under `BLOB_ROOT`.
- Payload bytes are installed before PostgreSQL commits metadata that references them.
- The worker and active Qdrant alias match the configured embedding fingerprint.
- Valkey loss does not strand durable work.
- Bounded inspection observes record, character, byte, and tokenizer limits.
- `doctor` is read-only.

For every change, run focused tests first, then the repository checks appropriate to the affected contract:

```bash
cd "<skill-root>"
ruff check .
ruff format --check .
env PYTHONDONTWRITEBYTECODE=1 \
  pytest -q -p no:cacheprovider scripts/
```

Changes touching acquisition, persistence, migration, indexing, recovery, documentation/parser contracts, or release verification also require the applicable disposable PostgreSQL, Qdrant, Valkey, worker/recovery, and bounded live-validation suites. Preserve Python 3.11 and 3.12 compatibility and record the exact tested commit SHA. Do not weaken a failing gate, convert an integration failure into a skip, or infer release readiness from a different commit.

For an aggregate release candidate, pull-request checks are necessary but not sufficient. After merge, resolve the exact resulting current `main` SHA, confirm its push-triggered gates, and dispatch `.github/workflows/release-campaign.yml` from `refs/heads/main` with `candidate-sha` equal to that SHA. Release acceptance requires exact identity among candidate, dispatch, workflow, and checked-out SHAs; successful campaign execution; successful strict authoritative verification; successful artifact upload; and final gate enforcement. Retain the workflow run ID, artifact ID, artifact digest, candidate SHA, and complete campaign evidence before closing the release gate or tagging/publishing.
