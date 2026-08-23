# Workflow State Schema

PostgreSQL is the sole authority for workflow state, invocation state, acquisition provenance, and corpus identities. Under Target A, immutable provider payload bytes remain in `BLOB_ROOT`. Qdrant and Valkey cannot advance workflow state.

## Authority and invariants

- `research_runs.state` is the authoritative lifecycle state.
- `research_runs.lifecycle_revision` is monotonic and used for compare-and-swap.
- `research_run_transitions` and `research_events` are append-only.
- Every persistent top-level operation records an authoritative `research_invocations` row.
- Search responses, stable candidates, extraction attempts, corpus identities, and jobs are PostgreSQL records.
- Idempotency keys reject conflicting reuse.
- Terminal runs reject new acquisition until explicitly reopened.
- Successful operation completion is read from committed service records, never a file-mediated handoff.
- Failed authoritative preflight occurs before Firecrawl construction or network invocation.

## Data dictionary

| Table or record family | Purpose | Primary invariants |
|---|---|---|
| `research_runs` | Current state for one run | state matrix, monotonic revision, execution-mode provenance |
| `research_run_transitions` | Immutable transition ledger | unique revision and idempotency key |
| `research_invocations` | Top-level and child operations | same-run parent, unique external ID, explicit terminal status |
| `research_events` | Ordered operational event stream | same-run invocation binding and stable ordering |
| `research_specs` | Versioned research scope | canonical payload and validation |
| `semantic_calls`, `semantic_artifacts` | Model or host decision provenance | model/input/schema identity and validation |
| `budget_snapshots` | Immutable resource authorization | run/spec/policy binding |
| `search_responses`, `search_candidates` | Retained provider search and stable selection | immutable response identity and candidate occurrence provenance |
| `extraction_attempts` | Direct and candidate extraction outcomes | item-level success/failure and lineage |
| sources, snapshots, documents, derivations, chunks | Corpus identity graph | content digest and versioned derivation identity |
| `research_run_assets` | Run-to-corpus provenance | explicit role and authoritative identity |
| embedding manifests and index jobs | Projection work | exact chunk/index-definition binding and lease safety |
| coverage, claims, evidence, audits | Research decision evidence | versioned, referentially valid, append-oriented records |

## State machine

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

`cancelled` is available from nonterminal states through explicit cancellation. `completed`, `partial`, `failed`, and `cancelled` are terminal.

## Wrapper boundary

For `fsearch` and `fscrape`:

1. validate argument and schema contracts;
2. validate database configuration, schema head, privileges, blob durability, and run eligibility;
3. create or resolve the authoritative invocation;
4. only then construct and invoke Firecrawl;
5. commit retained provider response, stable candidates or extraction attempts, corpus identities, blobs, manifests, and jobs;
6. read the committed authoritative result;
7. return bounded stable IDs and a stage-specific exit status.

A search response may commit before a later selected-extraction failure; the nonzero result includes the bounded committed partial identities. A multi-URL scrape preserves item order and item-level outcomes.

`fsearch_smart` persists plan, budget, provenance, events, acquisition, and resume checkpoints through the same PostgreSQL services. `--dry-run` is pure planning and does not enter the state machine.

## Replay, retry, and lifecycle continuation

- `finspect replay-search <response-id>` is read-only and verifies retained blob integrity.
- Repeating identical acquisition with the same idempotency key returns the original committed records without a duplicate provider call.
- `retry-candidates` creates explicit retry lineage and retries only failed items.
- A stale lifecycle revision requires a fresh status read.
- A new key denotes a genuinely new operation.

Use the current PostgreSQL run state plus the authoritative objective/window to choose continuation:

1. **Same objective and same authoritative time window, nonterminal run:** resume the same run from its persisted state. Do not create a replacement run merely because an earlier command stopped or a process restarted.
2. **Same objective/window, terminal run:** explicitly `frun reopen <fr_id>` only when continuing the same lifecycle is valid and intentional; a terminal run never resumes acquisition implicitly.
3. **Materially changed research objective or authoritative time window:** create a new run. Do not reopen an old run to make a changed scope appear to be the same lifecycle.

A `fsearch_smart` exit status of `75` is a deliberate resumable checkpoint. Preserve the same `fr_<uuid>`, inspect its state, clear or change the diagnostic stop condition, and resume that same run as a separate action. Never place `fsearch_smart`, `frun resume`, `frun seal-acquisition`, or another stateful lifecycle command in an unbounded retry loop.

Historical command churn must be described by its actual timestamps and run identities. Do not collapse runs from different UTC periods into one retry sequence merely because they investigated the same topic.

## RTK argv contract

`rtk proxy` forwards an argv vector; it does **not** shell-split `argv[0]`. The first argument after `rtk proxy` must therefore be one executable path/name, not a quoted string containing both an interpreter and script path.

Executable bundled entry points may be invoked directly:

```bash
rtk proxy "<skill-root>/scripts/fsearch_smart" "<topic>"
rtk proxy "<skill-root>/scripts/frun" status "$RUN_ID"
```

For a Python script that is not itself invoked as an executable entry point, pass the interpreter and script as separate argv elements:

```bash
rtk proxy python3 "<skill-root>/scripts/drain_index_jobs.py" --batch-size 64
```

**Wrong:** `rtk proxy "python3 <skill-root>/scripts/drain_index_jobs.py" --batch-size 64`.

RTK is only an efficiency wrapper. It does not alter the PostgreSQL/BLOB_ROOT authority boundary and its absence never authorizes direct Firecrawl MCP/SDK/HTTP substitution.

## Completion

After acquisition reaches `indexing`, `frun finish` verifies run-scoped projection work and advances through permitted coverage, synthesis, validation, and terminal transitions. It cannot jump from `created` to `completed`.

## Repair

```bash
scripts/research-db run-status '<fr-id>'
scripts/finspect operations --run '<fr-id>'
scripts/finspect invocations --run '<fr-id>'
scripts/finspect attempts --run '<fr-id>'
scripts/research-db verify-blobs
scripts/research-db doctor
```

`finspect operations --run <fr-id>` is the bounded read-only union view for provider-facing run history. It preserves `research_invocations` and `extraction_attempts` as distinct authoritative record kinds; use the narrower `invocations` and `attempts` commands when their table-specific contracts are required.

Never edit ledgers or synthesize state from exports, Qdrant, Valkey, or local files. Reopen is the supported path for intentional work after terminal state.

The clean schema head is `0038_postgres_authority`. See `migration-guide.md` for the exact legacy-tree import boundary.
