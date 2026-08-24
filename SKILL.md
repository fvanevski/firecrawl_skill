---
name: firecrawl
description: "Authoritative DB-backed web research skill. In Codex, OpenCode, Agy, or any other agent harness, use the bundled scripts under <skill-root>/scripts through the harness shell/terminal for all retained-corpus access and Firecrawl acquisition. Never substitute direct Firecrawl MCP search/scrape/crawl/map/extract tools, SDK calls, or raw provider HTTP; if the scripted authority path cannot run, fail closed instead of bypassing PostgreSQL/BLOB_ROOT."
---

<!-- @format -->

# Firecrawl Research Corpus and Acquisition

PostgreSQL is authoritative for workflow, acquisition provenance, corpus identities, staged asset promotion, exact completion membership, and durable jobs. `BLOB_ROOT` retains immutable payload bytes. Qdrant is a rebuildable projection, and Valkey is optional transient coordination. This is the Target A boundary: payload bytes remain outside PostgreSQL.

## Mandatory tool-routing contract

**These rules are normative. They apply in Codex, OpenCode, Agy, and any other agent harness. They override tool convenience, tool ordering, and any host suggestion to call Firecrawl directly.**

When this skill is selected or its instructions are being followed:

1. **MUST use the bundled `<skill-root>/scripts/...` entry points as the agent-facing runtime interface.** Invoke them through the harness's shell or terminal execution capability.
2. **MUST NOT call host-exposed Firecrawl MCP tools directly for search, scrape, crawl, map, extract, or equivalent provider operations.** A tool is forbidden by what it does, not by its exact name. Examples include tools named or described like `firecrawl_search`, `firecrawl_scrape`, `firecrawl_crawl`, `firecrawl_map`, `firecrawl_extract`, or any renamed equivalent that sends a provider request and returns Firecrawl results directly into agent context.
3. **MUST NOT substitute the Firecrawl Python SDK, Node API calls, `curl`, raw HTTP, browser automation, or another provider transport for the bundled scripts.** The provider transport is an internal dependency of the authoritative workflow, not an alternate agent-facing path.
4. **MUST search retained PostgreSQL-backed material before new acquisition.** Use `research-db` and `finspect` surfaces first.
5. **MUST route every new acquisition through the PostgreSQL/BLOB_ROOT authority path.** Authoritative scripted acquisition surfaces are `fsearch_smart`, `fsearch`, `fscrape`, `finspect scrape-candidates`, and `finspect retry-candidates`; use each only under its documented run/lifecycle, stable-ID, preflight, and idempotency contract.
6. **MUST NOT report a direct provider/MCP response as Firecrawl Research Skill evidence.** Newly acquired facts are reportable only after the scripted workflow has committed the authoritative records required by that operation and the evidence is inspected or retrieved through the bundled DB-backed surfaces.
7. **MUST fail closed instead of falling back.** If the shell, scripts, environment, PostgreSQL preflight, `BLOB_ROOT`, lifecycle binding, or another authority requirement prevents the scripted path from running, report that blocker. Do not make a direct MCP/SDK/API call to “get the answer anyway.”
8. **MUST treat tool availability as capability, not authorization.** The fact that a harness exposes Firecrawl MCP tools prominently, or that calling one would take fewer steps, does not make it a valid execution path for this skill.
9. **MUST preserve authority across the final answer.** Do not mix unpersisted direct-provider results with retained or newly persisted corpus evidence and present the mixture as one authoritative research result.

### Harness-independent routing

Use the shell/terminal primitive provided by the current harness. The harness may call it `shell`, `bash`, `terminal`, `exec`, or something else; that naming difference does not change the workflow. `rtk proxy` is an outer execution wrapper, not an authority boundary. Use it where available/configured as shown below. If RTK is unavailable but direct shell execution of the bundled scripts is permitted, run the same script directly; **never replace the script with a Firecrawl MCP call merely because RTK is unavailable.**

Use this routing table before selecting a tool:

| Need | Required agent-facing route |
|---|---|
| Check whether evidence already exists | `scripts/research-db corpus-overview`, `search-assets`, `fetch-passages` |
| Inspect runs, invocations, retained responses, attempts, candidates, or passages | `scripts/finspect` |
| Autonomous new web research | `scripts/fsearch_smart` |
| Inspect candidate-budget checks/config or authorize one persisted soft violation | `scripts/candidate-budget checks`, `override`, `config` |
| Controlled search in an explicitly prepared run | `scripts/frun` + `scripts/fsearch` |
| Controlled URL scraping in an explicitly prepared run | `scripts/frun` + `scripts/fscrape` |
| Acquire selected stable search candidates | `scripts/finspect scrape-candidates` |
| Retry a prior candidate acquisition by stable invocation ID | `scripts/finspect retry-candidates` |
| Run lifecycle, sealing, resume, finish, verification, audit status, comparison | `scripts/frun` |
| Direct Firecrawl MCP/SDK/API search or scrape | **Forbidden as a skill execution route** |

### Minimal decision procedure for agents

Follow this sequence literally unless the user asks for a narrower operation:

1. Resolve `<skill-root>` to the directory containing this file.
2. Search retained material with `research-db`/`finspect`.
3. If retained evidence is sufficient and current enough, retrieve bounded passages and answer from that authority.
4. If new acquisition is required, use `fsearch_smart` for autonomous research, the explicit `frun` + `fsearch`/`fscrape` curated sequence below for controlled new acquisition, or `finspect scrape-candidates` / `retry-candidates` when acquiring selected stable candidates or deliberately retrying prior candidate acquisition.
5. Inspect the resulting authoritative run/invocation and retrieve the committed evidence through `research-db`/`finspect` before presenting it as skill output.
6. If any authoritative step fails, stop at that failure and report it. Do not switch transports.

**Wrong:** call a Firecrawl MCP search/scrape tool, read the returned pages from agent context, and report those pages as the research result.

**Correct:** shell/terminal → bundled scripts → PostgreSQL/BLOB_ROOT authority → bounded DB-backed inspection/retrieval → report.

### Authority proof before reporting new acquisition

Before presenting newly acquired material as a successful result, the agent must be able to establish all applicable items below:

- the operation ran through a bundled script rather than a direct provider tool;
- the acquisition is bound to an authoritative `fr_<uuid>` run (including a run created by `fsearch_smart`);
- the relevant invocation/run state was persisted successfully;
- evidence being used is inspectable/retrievable through `research-db` or `finspect` rather than existing only in ephemeral agent tool output; and
- no failed preflight or lifecycle error was bypassed with another transport.

If those conditions are not satisfied, describe the operation as blocked or non-authoritative; do not silently downgrade the skill contract.

## Choose the first operation

1. Search retained material first with `corpus-overview`, `search-assets`, and bounded `fetch-passages`.
2. Use `finspect` for run history, retained-response replay, candidate selection, attempts, exact lexical or pattern search, and bounded inspection.
3. Acquire new evidence with `fsearch_smart`, `fsearch`, `fscrape`, or the stable-ID `finspect scrape-candidates` / `retry-candidates` acquisition paths only when retained evidence is absent, stale, incomplete, selected candidates require acquisition, a deliberate candidate retry is required, or the task explicitly requires current acquisition.
4. Inspect the authoritative run and invocation before retrying. Distinguish provider, parsing, ingestion, indexing, retrieval, blob-integrity reporting, audit scheduling or status, and comparison failures.
5. Never infer success or current state from a local path, presentation export, Qdrant point, Valkey message, zero-total blob report, or partial audit assessment.

Resolve `<skill-root>` to the directory containing this file. Prefer `rtk proxy` at the outer agent-visible boundary when RTK is available; if it is not, invoke the same bundled scripts directly through the harness shell/terminal. RTK availability never changes the authority route. The shell entry points automatically source `scripts/research-env` when it is readable unless `FIRECRAWL_RESEARCH_AUTO_ENV=0` is set deliberately. `research-env` also verifies that the selected interpreter imports `firecrawl_skill` from this exact checkout; source/runtime skew fails closed before authoritative work.

```bash
rtk proxy "<skill-root>/scripts/research-db" corpus-overview
rtk proxy "<skill-root>/scripts/research-db" search-assets "<query>" --limit 20
rtk proxy "<skill-root>/scripts/research-db" inspect-asset "<chunk-id>"
rtk proxy "<skill-root>/scripts/research-db" fetch-passages "<chunk-id>" --max-tokens 2000
```

`research-db fetch-passages` is explicitly chunk-only: its positional identities are PostgreSQL `chunks.id` UUIDs. For promotion subjects, search candidates, extraction attempts, sources, snapshots, documents, or derivations, use `finspect passages`; known non-chunk UUIDs are rejected with a structured detected/expected identity diagnostic rather than returning a misleading empty result.

## Authoritative direct acquisition

`fsearch` and `fscrape` require `DATABASE_URL`, an Alembic-head writable PostgreSQL store, a durable writable `BLOB_ROOT`, and a valid `fr_<uuid>` whose lifecycle has been explicitly prepared to `acquiring`. Their preflight completes before Firecrawl construction or network execution. A failed preflight cannot become a successful non-persistent acquisition.

Use a curated run when the operator or agent will choose the exact retained set. The canonical sequence is explicit and ordered:

```bash
RUN_ID="$(
  rtk proxy "<skill-root>/scripts/frun" start "<research objective>" \
    --run-mode curated \
    --mode autonomous_local
)"

rtk proxy "<skill-root>/scripts/frun" prepare "$RUN_ID"

rtk proxy "<skill-root>/scripts/fsearch" "<query>" \
  --research-run-id "$RUN_ID" \
  --limit 20 \
  --scrape-limit 0 \
  --sources web,news

rtk proxy "<skill-root>/scripts/fscrape" \
  "https://example.com/article-one" \
  "https://example.com/article-two" \
  --research-run-id "$RUN_ID"

# Discover the authoritative promotion-subject UUIDs for this exact run.
rtk proxy "<skill-root>/scripts/frun" assets "$RUN_ID"

# Explicitly retain or reject each intended asset before sealing. Use the
# returned promotion-subject `id`; snapshot IDs, ranks, URLs, and filenames are
# not valid substitutes.
rtk proxy "<skill-root>/scripts/frun" retain "$RUN_ID" "<promotion-subject-id>"
rtk proxy "<skill-root>/scripts/frun" reject "$RUN_ID" "<promotion-subject-id>" \
  --reason "not part of the curated evidence set"

rtk proxy "<skill-root>/scripts/frun" seal-acquisition "$RUN_ID"
rtk proxy "<skill-root>/scripts/frun" resume "$RUN_ID" --batch-size 64
rtk proxy "<skill-root>/scripts/frun" status "$RUN_ID"
```

Creating a run does not prepare it. `frun prepare` is the only normal direct-acquisition entry command. Beginning or completing `fsearch` or `fscrape` never changes lifecycle state implicitly.

Every production direct invocation records the exact locked lifecycle state and revision in PostgreSQL before provider execution:

- `fsearch` uses the shared direct-invocation start transaction and an `invocation_started` event;
- `fscrape` uses its specialized direct-scrape start transaction and a `direct_scrape_started` event; and
- both reject the operation without committing an invocation or start event if the locked state is not exactly `acquiring`.

A normal top-level operation receives a new `fc_<uuid>`. Use `--invocation-id` and the same idempotency key only for a deliberate retry of uncertain identical input. Conflicting key reuse fails closed.

`frun assets` is the curated-only, read-only discovery surface for promotion subjects. It returns the authoritative run state and revision plus stable subject IDs, snapshots, roles, stages, provenance, and promotion metadata. Use each subject's `id` with `retain` or `reject`; do not substitute a snapshot ID, candidate rank, URL, or local filename.

`retain` and `reject` are curated-only operations. The requested run, promotion subject, lifecycle revision, ownership check, and mutation are validated in one PostgreSQL transaction. A subject from another run is rejected even when both runs have the same lifecycle revision.

`seal-acquisition` is curated-only and performs no discovery or smart expansion. It advances the run through `extracting` to `indexing`, promotes retained subjects through evidence eligibility, and seals exact PostgreSQL completion membership. The transition and promotion steps are separately durable so an interrupted operation can resume safely.

If sealing is interrupted after the run reaches `indexing` but before an active membership seal exists:

```bash
rtk proxy "<skill-root>/scripts/frun" resume "$RUN_ID"
# next_action reports: frun seal-acquisition <fr_id>
rtk proxy "<skill-root>/scripts/frun" seal-acquisition "$RUN_ID"
rtk proxy "<skill-root>/scripts/frun" resume "$RUN_ID" --batch-size 64
```

In that state, `frun resume` does not start checkpoint processing and `frun finish` fails closed. Re-running `seal-acquisition` resumes the existing durable promotion/seal operation without repeating lifecycle transitions. Only an active membership seal permits checkpoint resume.

Direct acquisition is closed after sealing. Do not attempt to append another `fsearch` or `fscrape` while the run is `indexing` or later; the wrapper fails with the current state and explicit preparation guidance. A terminal run requires an explicit reopen before a new lifecycle.

`research-db worker --once` processes at most one bounded batch. For curated lifecycle work, use `frun resume`; it verifies mode and active membership before delegating to the bounded checkpoint workflow. The underlying drain evaluates the exact sealed census after every scoped worker batch. It never treats `claimed=0` as proof that live leases are complete. It waits with bounded backoff, reclaims expired leases, retries recoverable failures within the configured attempt budget, and succeeds only when every expected manifest is complete. Dead, missing-job, wrong-fingerprint, and manifest-inconsistent classes fail closed. A recoverable deadline or batch bound emits structured output and remains nonterminal. Unscoped worker use remains available for projection maintenance, but its queue-empty result is not run-completion evidence.

## Autonomous smart acquisition

`fsearch_smart` is the autonomous orchestration surface. It creates an authoritative autonomous run when one is not supplied. `--dry-run` is the only non-persistent execution surface and performs no database or network writes. `--spec-skeleton` is a separate no-database/no-network authoring surface that prints a valid `research-spec-v1` starting document; optionally provide a topic to seed its objective.

```bash
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>"
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" --research-run-id "$RUN_ID"
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" --dry-run
rtk proxy "<skill-root>/scripts/fsearch_smart" --spec-skeleton
```

The deterministic fallback accepts explicit ISO ranges, `past N days`, and bounded month-name ranges such as `August 18-23, 2026` or `from August 18 to August 23, 2026`. It does not add fuzzy date interpretation: ambiguous, impossible, reversed, or otherwise unsupported temporal wording fails closed and points to `schemas/research-workflow/research-spec-v1.json` plus `--spec-skeleton`.

Temporal intent in `fsearch_smart` is **semantic-primary and autonomous**: the smart objective model interprets the objective's temporal meaning and materializes it without delegating deterministic authority to the provider. The deterministic regex grammar is a **degraded/debug-only** fallback that engages only when the semantic model is unavailable and fails closed on anything outside its narrow grammar. **Provider recency is discovery-only** — it narrows which documents are fetched, never which passages qualify; discovery time is persisted as an independent, non-narrowing plan distinct from the ResearchSpec. **Publication and update provenance are distinct** (`datePublished` vs `dateModified`) and are tracked and classified separately. Scope **never relaxes automatically**: when authoritative passages cannot satisfy a persisted spec, the gap is classified and reported (exit `75`, next action `resolve_temporal_coverage_gap_then_resume_same_run`), and widening the scope requires an explicit ResearchSpec revision, not an in-run widening. See `references/acquisition-boundary.md` (temporal candidate authority) and `references/orchestration-boundary-remediation.md` (temporal coverage gaps and resume).

Do not use `fsearch_smart` to continue a curated run unless autonomous expansion has been explicitly requested and the lifecycle contract permits it. Curated `frun finish` never invokes smart expansion.

`fsearch_smart` uses one canonical result-disposition mapping. Successful terminal `completed` and `partial` results return `0`. `checkpoint`, `resumable`, and `operator_action_required` return `75`. `failed`, `cancelled`, explicit errors, and unrecognized nonterminal results return non-zero. Every invocation prints `Run ID`, `Final state`, `Orchestrator outcome`, and `Next action`; do not infer process semantics from only one of those fields.

A status of `75` is an intentional recoverable result, not a generic failure and not permission to retry automatically. Preserve the printed `Run ID`, inspect its PostgreSQL state once, and follow the printed `Next action`. For a checkpoint/resumable result, resume the same run only after clearing the reason that produced the checkpoint. For `operator_action_required`, resolve the exact persisted soft candidate-budget gate first:

```bash
rtk proxy "<skill-root>/scripts/candidate-budget" checks "$RUN_ID"
rtk proxy "<skill-root>/scripts/candidate-budget" override \
  "$RUN_ID" "<budget-check-id>" "<soft-limit-name>" \
  --reason "<justification>" --author "<operator>"
rtk proxy "<skill-root>/scripts/fsearch_smart" "<same topic>" \
  --research-run-id "$RUN_ID"
```

Use only `scripts/candidate-budget`; direct execution of `candidate_budget_cli.py` is an internal-entrypoint error because it would bypass wrapper runtime provenance. Candidate-budget overrides are exact-check, exact-lifecycle-revision, exact-scope/fingerprint authorizations. Hard violations are non-overridable, and changed completion membership requires a new exact check rather than inheriting a stale override.

```bash
# fsearch-smart-checkpoint-handler:start
set +e
SMART_OUTPUT="$(
  rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" 2>&1
)"
SMART_STATUS=$?
set -e

printf '%s\n' "$SMART_OUTPUT"
RUN_ID="$(sed -n 's/^Run ID: //p' <<<"$SMART_OUTPUT" | tail -n 1)"

case "$SMART_STATUS" in
  0)
    test -n "$RUN_ID"
    ;;
  75)
    test -n "$RUN_ID"
    rtk proxy "<skill-root>/scripts/research-db" run-status "$RUN_ID"
    printf '%s\n' \
      "Recoverable smart-run result. Follow the printed Next action using this same run." >&2
    exit 75
    ;;
  *)
    exit "$SMART_STATUS"
    ;;
esac
# fsearch-smart-checkpoint-handler:end
```

When checkpoint continuation is intended, perform it separately. `FIRECRAWL_SMART_STOP_AFTER_STATE` is an internal test or diagnostic control; a normal continuation must not retain the same stop condition that produced the checkpoint.

```bash
# fsearch-smart-checkpoint-resume:start
unset FIRECRAWL_SMART_STOP_AFTER_STATE
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>" \
  --research-run-id "$RUN_ID"
# fsearch-smart-checkpoint-resume:end
```

Planning, budget, provenance, semantic artifacts, and resume checkpoints remain PostgreSQL records. Do not reconstruct a checkpoint from console output or a local file, create a replacement run, or place an unbounded retry loop around a stateful command.

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

`finspect inspect` and `finspect passages` resolve PostgreSQL identity domains explicitly. Supported higher-level identities include promotion subject, search candidate, extraction attempt, source, snapshot, document, derivation, and chunk. The response reports the supplied `identity_type` plus related authoritative IDs. The crosswalk follows PostgreSQL relations only; it never asks Qdrant to infer identity and rejects ambiguous cross-domain UUID membership rather than assigning precedence.

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

`replay-search` verifies retained byte length and SHA-256 before returning the payload and never calls Firecrawl. `scrape-candidates` and `retry-candidates` are authoritative acquisition surfaces permitted by the mandatory routing contract; they perform the same authoritative preflight as direct acquisition and must be used with their stable candidate/invocation identities.

## Structured scrape

Structured scrape follows the same explicit curated lifecycle:

```bash
SCRAPE_RUN_ID="$(
  rtk proxy "<skill-root>/scripts/frun" start "structured scrape" \
    --run-mode curated \
    --mode autonomous_local
)"
rtk proxy "<skill-root>/scripts/frun" prepare "$SCRAPE_RUN_ID"
rtk proxy "<skill-root>/scripts/fscrape" "https://example.com/product" \
  --research-run-id "$SCRAPE_RUN_ID" \
  --schema '{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}' \
  --json
rtk proxy "<skill-root>/scripts/frun" assets "$SCRAPE_RUN_ID"
# Explicitly retain or reject the returned promotion subject, then:
rtk proxy "<skill-root>/scripts/frun" seal-acquisition "$SCRAPE_RUN_ID"
rtk proxy "<skill-root>/scripts/frun" resume "$SCRAPE_RUN_ID" --batch-size 64
```

Structured provider output is validated before successful document ingestion. Invalid structured output remains a failed extraction attempt; it is not silently accepted.

## Workflow and completion

The following is a common curated acquisition path, not the complete transition graph:

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

For a curated run, seal exact acquisition membership first, then resume indexing, inspect authoritative status, and finish explicitly:

```bash
rtk proxy "<skill-root>/scripts/frun" seal-acquisition "$RUN_ID"
rtk proxy "<skill-root>/scripts/frun" resume "$RUN_ID" --batch-size 64
rtk proxy "<skill-root>/scripts/frun" status "$RUN_ID"
rtk proxy "<skill-root>/scripts/frun" finish "$RUN_ID" --outcome satisfied
rtk proxy "<skill-root>/scripts/frun" status "$RUN_ID"
```

`frun finish` requires an active exact membership seal and complete run-scoped index evidence. It advances through coverage review, synthesis, validation, and the requested terminal outcome without autonomous acquisition.

Retry an uncertain command with its original idempotency key. After a stale revision, inspect `run-status` before deciding whether a new command is valid. Reopen terminal runs explicitly.

## Blob-integrity reporting, audit scheduling, and comparison

Use the run wrapper for invocation-output blob-integrity reports, scheduling or inspecting audit assessment records, and cross-run comparison:

```bash
rtk proxy "<skill-root>/scripts/frun" verify "$RUN_ID"
rtk proxy "<skill-root>/scripts/frun" audit "$RUN_ID"
rtk proxy "<skill-root>/scripts/frun" audit-status "$RUN_ID"
rtk proxy "<skill-root>/scripts/frun" compare "$RUN_ID" "<other-run-id>"
```

`frun verify` scans invocation output `results` for `snapshot` or `artifacts` values containing blob `path` and `sha256` pairs. Each eligible pair contributes to exactly one detailed counter: `available` when the content-addressed blob exists and verifies, `missing` when the expected digest is absent from `BLOB_ROOT`, or `hash_mismatch` when the digest path exists but its bytes do not match the expected SHA-256. Thus `total` is the sum of those three counters. File-only legacy paths are reported separately as `file_based_unverified` and do not make a zero-eligible report conclusive. It does not validate terminal state, claims, evidence packets, synthesis, declared outcome, or run completion. As a result, it is not evidence that the run completed or passed.

The structured integrity `status` is `passed` only when at least one eligible pair was examined and all are available, `failed` when any eligible pair is missing or hash-mismatched, and `inconclusive` when no eligible path/hash pairs were examined. A `total: 0` report is therefore never a successful integrity proof. For backward compatibility, the process exit code is a control-flow signal rather than the integrity verdict: conclusive `passed` and `failed` reports exit `0`; `inconclusive` exits `1` by default; and `--allow-empty` changes only an inconclusive result to exit `0`. Automation that needs the integrity verdict must inspect the JSON `status` and counters rather than infer it from exit code alone. Inspect `frun status` and the relevant evidence or terminal-decision records separately.

`frun audit` currently schedules and persists an audit assessment identity with status `partial`. It does not invoke a semantic provider or execute deterministic audit-stage validation. `frun audit-status` reports the latest stored assessment, which may therefore be only a scheduled partial record rather than a completed semantic evaluation. Provider, model, and stage choices label the scheduled record; accepted controls such as force, call limits, input-token limits, and fallback settings are not evidence that evaluation occurred and are not consumed by the current scheduling path. Do not present either command as authoritative completion verification or completed semantic-audit assurance.

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

The unscoped drain in this projection-recovery sequence is maintenance evidence only. Before activation, independently verify PostgreSQL manifest/job completion and Qdrant reconciliation. Never embed a query against an alias backed by a different embedding fingerprint. Retrieval falls back to PostgreSQL lexical search and `doctor` reports the mismatch.

## Explicit exports

```bash
rtk proxy "<skill-root>/scripts/research-db" export-invocation "fc_<uuid>" --output invocation.json
rtk proxy "<skill-root>/scripts/research-db" export-run "$RUN_ID" --output run.json
```

Exports are never replay, retry, selection, ingestion, or workflow inputs.

## Documentation

- `references/audit-remediation-305.md`: runtime provenance, smart-result/recovery, candidate-budget, temporal fallback, identity-domain, first-byte retry, and planner-diagnostic contracts introduced by issue #305.
- `references/curated-run-lifecycle.md`: canonical autonomous/curated mode, direct-invocation provenance, promotion-subject discovery, run-scoped promotion, sealing, and interruption repair.
- `references/authoritative-workflows.md`: canonical acquisition, completion, transaction, and projection-recovery sequences.
- `references/research-store-architecture.md`: Target A authority and consistency.
- `references/operations-runbook.md`: deployment, backup, restore, worker, projection, and recovery.
- `references/research-store-operations.md`: compact operator commands.
- `references/migration-guide.md`: schema and legacy-tree migration.
- `references/recovery-drill-checklist.md`: executable recovery drills.
- `references/cli-script-disambiguation.md`: Node CLI, Python SDK, and MCP boundaries.
- `references/coding-agent-guide.md`: implementation and testing constraints.
- `references/search-relational-provenance.md`: relational provider invocation/attempt and smart-plan provenance, migration compatibility, bounded idempotency, and failure cleanup.
- `references/workflow-state-schema.md`: complete lifecycle and invocation contracts.
- `references/release-notes-rc9.md`: breaking runtime and legacy-migration compatibility boundary retained by RC-10.
- `references/release-candidate-gate-rc10.md`: aggregate exact-head gate and mandatory post-merge campaign boundary.
- `references/release-campaign-timing-diagnostics.md`: strict PostgreSQL-bound timing-evidence contract used by the credentialed release campaign.

RC-9 remains the controlling breaking-change and migration boundary. RC-10 adds aggregate and credentialed release validation without introducing a schema migration, payload relocation, Qdrant authority, or Valkey correctness dependency. Issue #212 adds only additive run-mode and invocation-provenance JSONB fields plus explicit CLI lifecycle commands; historical records remain uninferred.

## Verification

A supported acquisition reports stable authoritative IDs or a stage-specific failure. In addition:

- No Firecrawl or network invocation occurs after failed authoritative preflight.
- Direct invocation start state and revision are written under the authoritative run lock.
- Direct acquisition outside `acquiring` commits no invocation or start event.
- `frun assets` returns stable, run-scoped promotion-subject IDs before retain/reject.
- Retain/reject cannot cross run ownership boundaries.
- Checkpoint resume and finish require an active exact membership seal.
- PostgreSQL identities resolve to immutable bytes under `BLOB_ROOT`.
- Payload bytes are installed before PostgreSQL commits metadata that references them.
- The worker and active Qdrant alias match the configured embedding fingerprint.
- Valkey loss does not strand durable work.
- Bounded inspection observes record, character, byte, and tokenizer limits.
- `doctor` is read-only.

For every change, run focused tests first, then the repository checks appropriate to the affected contract:

```bash
cd "<skill-root>"
rtk proxy ruff check .
rtk proxy ruff format --check .
rtk proxy env PYTHONDONTWRITEBYTECODE=1 \
  pytest -q -p no:cacheprovider tests/
```

Changes touching acquisition, persistence, migration, indexing, recovery, documentation/parser contracts, or release verification also require the applicable disposable PostgreSQL, Qdrant, Valkey, worker/recovery, concurrency, restart/resume, and bounded live-validation suites. Preserve Python 3.11 and 3.12 compatibility and record the exact tested commit SHA. Do not weaken a failing gate, convert an integration failure into a skip, or infer release readiness from a different commit.

For an aggregate release candidate, pull-request checks are necessary but not sufficient. After merge, resolve the exact resulting current `main` SHA, confirm its push-triggered gates, and dispatch `.github/workflows/release-campaign.yml` from `refs/heads/main` with `candidate-sha` equal to that SHA. Release acceptance requires exact identity among candidate, dispatch, workflow, and checked-out SHAs; successful campaign execution; successful strict authoritative verification; successful artifact upload; and final gate enforcement. Retain the workflow run ID, artifact ID, artifact digest, candidate SHA, and complete campaign evidence before closing the release gate or tagging/publishing.
