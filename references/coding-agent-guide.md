<!-- @format -->

# Coding Agent Guide

Authoritative reference for coding agents working on the Firecrawl Research Skill platform. This guide explains the architecture, execution modes, budget policy, state machine, retrieval, evidence, synthesis, cache, and configuration so that agents can modify, debug, and extend the system correctly.

## Table of contents

1. [Architecture overview](#1-architecture-overview)
2. [Authority boundaries](#2-authority-boundaries)
3. [Execution modes](#3-execution-modes)
4. [Budget policy](#4-budget-policy)
5. [Coverage-led research](#5-coverage-led-research)
6. [State machine](#6-state-machine)
7. [Extraction and derivations](#7-extraction-and-derivations)
8. [Retrieval and evidence](#8-retrieval-and-evidence)
9. [Report synthesis and validation](#9-report-synthesis-and-validation)
10. [Cache behavior](#10-cache-behavior)
11. [Embedding microbatching](#11-embedding-microbatching)
12. [Resource governance](#12-resource-governance)
13. [Configuration variables](#13-configuration-variables)
14. [Coding conventions](#14-coding-conventions)
15. [Testing guidance](#15-testing-guidance)

---

## 1. Architecture overview

### 1.1 High-level flow

```
User / Host Agent
        |
        v
Research Orchestrator (fsearch_smart)
        |
        +--> Firecrawl (search + scrape)
        |
        +--> PostgreSQL (authoritative state)
        |      - research_runs, invocations, events
        |      - sources, snapshots, documents, chunks
        |      - budgets, coverage, claims, evidence
        |      - semantic calls, artifacts, audits
        |
        +--> Blob root (immutable payloads)
        |
        +--> Qdrant (rebuildable vector index)
        |
        +--> Valkey (transient wakeups)
        |
        +--> Scratch / Catalog v5 (derived artifacts)
```

### 1.2 Key components

| Component               | Location                              | Role                                    |
| ----------------------- | ------------------------------------- | --------------------------------------- |
| `ResearchOrchestrator`  | `scripts/fsearch_smart`               | Coverage-led research orchestration     |
| `AcquisitionService`    | `scripts/research_store/service.py`   | Search and scrape execution             |
| `ResearchRunService`    | `scripts/research_store/service.py`   | Workflow state machine, transitions     |
| `BudgetPolicy`          | `scripts/budget_policy.py`            | Deterministic resource caps             |
| `PostgresUnitOfWork`    | `scripts/research_store/postgres.py`  | Database transactions, idempotency      |
| `IndexWorker`           | `scripts/research_store/indexing.py`  | Embedding and Qdrant projection         |
| `RetrievalService`      | `scripts/research_store/retrieval.py` | Hybrid retrieval (FTS + dense + rerank) |
| `EvidencePacketService` | `scripts/research_store/evidence.py`  | Evidence packet construction            |
| `SemanticCallService`   | `scripts/research_store/service.py`   | Model call provenance and artifacts     |
| `CatalogService`        | `scripts/catalog_v5.py`               | Catalog v5 compatibility exports        |

### 1.3 Directory structure

```
scripts/
├── fsearch_smart           # Research orchestrator
├── fsearch                 # Single-query search
├── fscrape                 # URL scrape
├── fread                   # Scratch file inspector
├── frun                    # Research run lifecycle
├── research-db             # Database operations CLI
├── budget_policy.py        # Budget policy engine
├── catalog_v5.py           # Catalog v5 audit records
├── classifier.py           # (Retired — legacy)
├── cleanup.py              # Markdown cleanup pass
├── model_gateway.py        # Model transport abstraction
├── research_workflow.py    # Legacy workflow adapter
├── shadow_comparison.py    # Legacy adapter comparison
├── live_validate.py        # Live validation harness
├── generate_benchmark_fixtures.py  # Benchmark fixture generation
├── research_store/         # Core Python module
│   ├── __init__.py
│   ├── cli.py              # CLI entry point (research-db)
│   ├── config.py           # StoreConfig
│   ├── container.py        # Service factory
│   ├── postgres.py         # PostgreSQL connection, UoW
│   ├── service.py          # ResearchRunService, semantic services
│   ├── indexing.py         # IndexWorker, OpenAICompatibleEmbedder
│   ├── qdrant.py           # QdrantIndex
│   ├── retrieval.py        # RetrievalService, CohereCompatibleReranker
│   ├── blob.py             # ContentAddressedBlobStore
│   ├── queue.py            # ValkeyQueue
│   ├── domain.py           # IngestRequest
│   ├── alembic/            # Migrations
│   └── versions/           # Migration files (0001–0033)
├── research_domain/        # Domain models and schemas
├── test_*.py               # Unit and integration tests
└── research-env            # Environment setup script
```

---

## 2. Authority boundaries

### 2.1 What is authoritative

| Layer                 | Authority                                                            | Recovery                                     |
| --------------------- | -------------------------------------------------------------------- | -------------------------------------------- |
| **PostgreSQL**        | Workflow state, corpus, events, budgets, audits, semantic provenance | Restore first; never infer from other layers |
| **Blob root**         | Immutable payload bytes                                              | Restore with PostgreSQL; verify hashes       |
| **Qdrant**            | Dense-retrieval projection                                           | Rebuild from PostgreSQL chunks               |
| **Valkey**            | Transient wakeups                                                    | Loss is safe; worker polls PostgreSQL        |
| **Scratch / Catalog** | Derived artifacts                                                    | Regenerate from PostgreSQL + blobs           |

### 2.2 Deterministic versus semantic authority

**Deterministic code owns:**

- Identity allocation (UUIDs, IDs)
- State transitions (with compare-and-swap)
- Budget enforcement
- Schema validation
- Content hashing
- Transaction boundaries
- Index activation/pruning

**Semantic authority (LLM / host agent) owns:**

- Research spec interpretation
- Search query proposals
- Candidate relevance assessment
- Evidence role classification
- Coverage assessment
- Report drafting

**Rule:** Semantic output must be schema-validated and reference-validated before persistence. Semantic decisions never mutate state directly — they produce structured proposals that deterministic code validates and commits.

### 2.3 Idempotency

Every state mutation uses idempotency keys:

```python
# Same idempotency key + same revision = returns original record
# Conflicting reuse = rejected
```

Retried commands must reuse their original idempotency key. After a stale-revision error, read status and decide whether a genuinely new command is still valid.

---

## 3. Execution modes

### 3.1 Mode selection

| Mode                  | Semantic authority     | Inner LLM calls                    | Default source      |
| --------------------- | ---------------------- | ---------------------------------- | ------------------- |
| `agent_led`           | Host agent             | Absent when host decision exists   | Host-facing service |
| `autonomous_local`    | Local LLM              | Each stage independently retryable | Standalone CLI      |
| `deterministic_debug` | Deterministic fixtures | None                               | Tests               |

### 3.2 Mode changes

Mode changes are append-only events with compare-and-swap:

```bash
rtk proxy "<skill-root>/scripts/research-db" run-mode-change \
  "<run-id>" "autonomous_local" \
  --expected-revision 3 \
  --idempotency-key "mode-change-$(uuidgen)" \
  --requested-by "operator" \
  --approved-by "operator" \
  --reason "switch to local synthesis"
```

Terminal runs must be reopened before changing mode. Changes invalidate prior valid semantic artifacts.

### 3.3 Agent behavior by mode

**`agent_led`:**

- The agent supplies or approves `ResearchSpec`.
- The agent makes semantic decisions when requested.
- The skill validates proposals and persists state.
- No inner LLM calls when a valid host-agent decision exists.

**`autonomous_local`:**

- The local LLM generates `ResearchSpec`, search plan, candidate assessments, coverage assessment, evidence mapping, and report draft.
- Each stage is independently retryable.
- The skill enforces all state changes and hard limits.

---

## 4. Budget policy

### 4.1 Policy engine

`BudgetPolicy` (in `scripts/budget_policy.py`) maps a validated `ResearchSpec` to deterministic resource caps:

| Tier        | Risk   | Search branches | Results/branch | Extraction attempts | Successes required |
| ----------- | ------ | --------------- | -------------- | ------------------- | ------------------ |
| `focused`   | Low    | 2               | 15             | 8                   | 6                  |
| `standard`  | Medium | 3               | 25             | 18                  | 12                 |
| `intensive` | High   | 5               | 40             | 36                  | 25                 |

### 4.2 Policy inputs

The policy inspects:

- Research archetype (news, analysis, monitoring, etc.)
- Risk level (low, medium, high)
- Question and claim counts
- Freshness requirements
- Corroboration and contradiction requirements
- Required source minima

**Not policy inputs:** Objective word count, topic length, legacy `--complexity` label.

### 4.3 Budget snapshots

Every budget authorization creates an immutable snapshot in `research_budget_snapshots`:

- `(run_id, policy_version, run_revision)` is the composite key.
- Same key with different content requires a new policy version or explicit revision.
- User limits may only tighten policy caps — looser values fail.

### 4.4 Coding guidance

- Never bypass `BudgetPolicy.authorize()`.
- User-provided limits (`--max-searches`, etc.) are stricter caps, not policy inputs.
- Budget snapshots are append-only — never update or delete.
- When the budget is exceeded, the orchestrator stops and records the reason.

---

## 5. Coverage-led research

### 5.1 Coverage ledger

The coverage ledger is derived from immutable `coverage_events` but exposed as a current-state projection:

```json
{
  "schema_version": "coverage-ledger-v1",
  "run_id": "uuid",
  "revision": 1,
  "items": [
    {
      "coverage_item_id": "uuid",
      "item_type": "question|claim|source_requirement|freshness_requirement|...",
      "subject_id": "string",
      "status": "missing|candidate_identified|acquired|partially_supported|supported|contradicted|qualified|satisfied|blocked|waived|unassessed",
      "candidate_ids": [],
      "snapshot_ids": [],
      "passage_ids": [],
      "independent_source_count": 0,
      "required_independent_source_count": 0,
      "authority_classes_present": [],
      "freshness_status": "satisfied|unsatisfied|uncertain|not_applicable",
      "remaining_gap": "string",
      "confidence": 0.0
    }
  ],
  "overall_status": "insufficient|partial|sufficient|blocked|unassessed"
}
```

### 5.2 Stopping decisions

Stopping is based on coverage, **not** on:

- Successful page count
- Candidate count
- Word count
- Elapsed iterations

Every additional acquisition must identify:

1. The unresolved coverage gap it addresses.
2. The expected contribution.
3. The hard budget it consumes.
4. The decision authority that proposed it.
5. The deterministic policy checks that authorized it.

### 5.3 Strategy revisions

When coverage gaps persist, the orchestrator may propose a strategy revision:

```json
{
  "schema_version": "strategy-revision-v1",
  "run_revision": 1,
  "decision": "search|scrape|retrieve|synthesize|stop_partial|stop_failed",
  "target_coverage_item_ids": [],
  "proposed_queries": [],
  "expected_contribution": "string",
  "rationale": "string",
  "confidence": 0.0
}
```

---

## 6. State machine

### 6.1 States and transitions

```
created -> planning -> corpus_review -> acquiring -> extracting -> indexing ->
coverage_review -> retrieving -> synthesizing -> validating ->
completed | partial | failed

cancelled (from any non-terminal state)
```

**Permitted transitions:**

| From              | To                                                                           |
| ----------------- | ---------------------------------------------------------------------------- |
| `created`         | `planning`, `failed`                                                         |
| `planning`        | `corpus_review`, `failed`                                                    |
| `corpus_review`   | `acquiring`, `retrieving`, `failed`                                          |
| `acquiring`       | `coverage_review`, `extracting`, `failed`                                    |
| `extracting`      | `indexing`, `coverage_review`, `failed`                                      |
| `indexing`        | `coverage_review`, `partial`, `failed`                                       |
| `coverage_review` | `acquiring`, `extracting`, `retrieving`, `synthesizing`, `partial`, `failed` |
| `retrieving`      | `coverage_review`, `synthesizing`, `failed`                                  |
| `synthesizing`    | `validating`, `failed`                                                       |
| `validating`      | `completed`, `partial`, `failed`                                             |

**Terminal states** (`completed`, `partial`, `failed`, `cancelled`) reject all ordinary transitions.

### 6.2 Transition mechanics

Transitions use compare-and-swap on `lifecycle_revision`:

```bash
rtk proxy "<skill-root>/scripts/research-db" run-transition \
  "<run-id>" "<next-state>" \
  --expected-revision <N> \
  --idempotency-key "<unique-key>" \
  --actor "<actor-type>"
```

- The revision is read, then used as input.
- A stale revision error means another command committed first.
- Retried commands reuse their original idempotency key.

### 6.3 Reopening

```bash
rtk proxy "<skill-root>/scripts/research-db" run-reopen "<run-id>" \
  --reason "add missing corroboration"
```

Reopen moves a terminal run to `created`, increments revision, records `reopened_from_revision`, and invalidates prior valid semantic artifacts.

---

## 7. Extraction and derivations

### 7.1 Extraction pipeline

```
Firecrawl scrape result
  -> immutable snapshot (content hash)
  -> normalized document (parser version)
  -> blocks (hierarchical)
  -> chunks (chunker version)
  -> embedding manifest -> indexing job -> Qdrant point
```

### 7.2 Derivation versions

Each document derivation is identified by:

- Parser version
- Normalization version
- Chunker version
- Document hash

Changed content links to the prior snapshot. Multiple normalized documents can exist for one snapshot.

### 7.3 Redrive

After parser, normalization, or chunker changes:

```bash
# Redrive all documents
rtk proxy "<skill-root>/scripts/research-db" rederive --all

# Redrive a specific snapshot
rtk proxy "<skill-root>/scripts/research-db" rederive --snapshot "<snapshot-id>"

# Redrive with explicit versions
rtk proxy "<skill-root>/scripts/research-db" rederive-v2 --all \
  --parser-version "v2" --chunker-version "v3" --activate
```

**Never manufacture a new snapshot for a parser or chunker upgrade.** Use `rederive` to rebuild from blob bytes.

### 7.4 Export

```bash
# Export invocation to _corpus.json
rtk proxy "<skill-root>/scripts/research-db" export-invocation "fc_<uuid>" --output _corpus.json

# Export entire run
rtk proxy "<skill-root>/scripts/research-db" export-run "<run-id>" --output run-export.json
```

---

## 8. Retrieval and evidence

### 8.1 Hybrid retrieval

Retrieval combines four signals:

1. **PostgreSQL full-text search** — lexical matching
2. **Qdrant dense search** — semantic similarity
3. **Reciprocal-rank fusion (RRF)** — combines lexical and dense scores
4. **Local reranker** — re-ranks top candidates

On fingerprint mismatch, dense retrieval is skipped and lexical fallback is used. `doctor` reports the mismatch as unhealthy.

### 8.2 Retrieval commands

```bash
# Search assets (compact manifests)
rtk proxy "<skill-root>/scripts/research-db" search-assets "<query>" --limit 20

# Inspect a candidate
rtk proxy "<skill-root>/scripts/research-db" inspect-asset "<candidate-id>"

# Fetch bounded passages
rtk proxy "<skill-root>/scripts/research-db" fetch-passages "<candidate-id>" --max-tokens 2000
```

### 8.3 Evidence packets

Evidence packets are the exclusive evidence input for report synthesis:

```json
{
  "schema_version": "evidence-packet-v1",
  "run_id": "uuid",
  "research_spec_id": "uuid",
  "coverage_revision": 1,
  "claims": [],
  "passages": [],
  "claim_evidence_bindings": [],
  "corroborating_groups": [],
  "contradicting_groups": [],
  "qualifying_groups": [],
  "near_duplicate_groups": [],
  "source_diversity_summary": {},
  "freshness_summary": {},
  "limitations": [],
  "unresolved_items": [],
  "retrieval_provenance": []
}
```

**Every report claim must resolve to exact packet passages.** Unsupported and qualified claims remain explicit. A report cannot complete against a stale packet.

---

## 9. Report synthesis and validation

### 9.1 Synthesis stages

Autonomous-local synthesis proceeds through bounded stages:

| Stage           | Purpose                                    |
| --------------- | ------------------------------------------ |
| `outline`       | Generate report outline from evidence      |
| `binding`       | Map claims to evidence passages            |
| `draft`         | Draft report content                       |
| `citation_pass` | Validate all citations resolve to passages |

Each stage is independently retryable and resumable.

### 9.2 Citation validation

Every citation in a report must resolve to an exact passage in the evidence packet:

- Passage IDs must exist in the packet.
- Claim-to-evidence bindings must be valid.
- Corroborating and contradicting groups must be populated.

Invented citations are rejected. Unsupported claims are marked as `unsupported`.

### 9.3 Stale packet handling

If the evidence packet revision changes after synthesis begins:

1. The synthesis stage detects the stale revision.
2. The stage is marked `failed`.
3. The orchestrator rebuilds the packet and retries.

---

## 10. Cache behavior

### 10.1 Semantic cache

The semantic cache (`semantic_cache` table) stores:

- Input hash (prompt + model + schema)
- Output artifact reference
- Policy version
- Reference hash (for revalidation)

### 10.2 Cache key identity

Cache keys are based on:

- Model name and revision
- Prompt version
- Input hash (UTF-8 JSON, sorted keys, compact separators)
- Schema name and version

Identical inputs always produce identical cache keys.

### 10.3 Cache invalidation

Cache entries are invalidated when:

- The policy version changes.
- The reference hash changes (schema, prompt, or model changed).
- An explicit invalidation command is issued.

### 10.4 Cache loss

Cache loss **cannot** lose authoritative workflow state:

- Cache is a performance optimization, not a state store.
- Misses result in normal model transport.
- No workflow transition depends on cache presence.

### 10.5 Cache revalidation

Cached results must be revalidated against:

- Current reference schemas.
- Current budget policy.
- Current evidence packet revision.

---

## 11. Embedding microbatching

### 11.1 Lease-safe microbatching

The `IndexWorker` processes embedding jobs with lease safety:

1. Claim pending jobs with `FOR UPDATE SKIP LOCKED`.
2. Process in batches (default: 32).
3. Each job has a lease token, owner, expiration, and attempt count.
4. Stale lease tokens are rejected.
5. Expired final attempts move to dead-letter state.

### 11.2 Partial batch failure

Partial batch failure **cannot** falsely complete jobs:

- Each job is keyed by `(manifest_id, index_definition_id)`.
- Failed jobs remain in the queue for retry.
- Successful jobs are committed atomically with their manifest.

### 11.3 Vector dimension mismatch

If the embedding dimension changes:

1. A new embedding fingerprint is generated.
2. A new physical collection is created.
3. The alias is not switched until verification passes.
4. Existing queries against the old alias fall back to lexical search.

---

## 12. Resource governance

### 12.1 Endpoint health

The `model_endpoints` table tracks health for each endpoint:

| Field                 | Purpose                                       |
| --------------------- | --------------------------------------------- |
| `endpoint_name`       | `generative`, `embedding`, or `reranker`      |
| `status`              | `healthy`, `degraded`, `unhealthy`, `unknown` |
| `concurrent_requests` | Active request count                          |
| `queued_requests`     | Pending request count                         |
| `degraded_since`      | When degradation began                        |
| `restart_count`       | Number of restarts detected                   |

### 12.2 Backpressure

When an endpoint is `degraded` or `unhealthy`:

1. New requests are queued (up to a concurrency limit).
2. Requests beyond the limit are rejected with a resource-limited error.
3. The orchestrator records the resource limitation and may retry later.

### 12.3 Endpoint restart

When an endpoint restarts:

1. Health checks detect the change.
2. `restart_count` is incremented.
3. In-flight requests fail — they are retried by the worker.
4. No state is corrupted.

### 12.4 Commands

```bash
# Check endpoint health
rtk proxy "<skill-root>/scripts/research-db" endpoint-health

# Check resource status
rtk proxy "<skill-root>/scripts/research-db" resource-status
```

---

## 13. Configuration variables

See `operations-runbook.md#5-configuration-variables` for the complete list of all configuration variables, their defaults, effects, and constraints.

Key variables for coding agents:

| Variable                        | Default                              | Effect                |
| ------------------------------- | ------------------------------------ | --------------------- |
| `FIRECRAWL_RESEARCH_PERSIST`    | `auto`                               | Persistence mode      |
| `DATABASE_URL`                  | Derived                              | PostgreSQL connection |
| `BLOB_ROOT`                     | `$HOME/.local/share/firecrawl/blobs` | Blob storage root     |
| `QDRANT_URL`                    | `http://127.0.0.1:6333`              | Qdrant endpoint       |
| `VALKEY_URL`                    | Derived                              | Valkey endpoint       |
| `FIRECRAWL_LLM_LOCAL_BASE_URL`  | `http://192.168.4.115:8002/v1`       | Local LLM             |
| `EMBEDDING_URL`                 | Derived                              | Embedding endpoint    |
| `RERANKER_URL`                  | Derived                              | Reranker endpoint     |
| `FIRECRAWL_CATALOG_DISABLED`    | unset                                | Disable catalog       |
| `FIRECRAWL_AUDIT_AUTO_SEMANTIC` | `1`                                  | Auto LLM audits       |
| `FIRECRAWL_LEGACY_ADAPTER_MODE` | `compatibility`                      | Legacy adapter mode   |

---

## 14. Coding conventions

### 14.1 Type annotations required

All new public functions must have type annotations. Follow the existing pattern in the codebase:

```python
def process_chunk(
    chunk_id: UUID,
    content: bytes,
    index_definition: IndexDefinition,
) -> EmbeddingResult: ...
```

### 14.2 Thin shell entry points

Shell scripts and CLI entry points should be thin wrappers around typed Python modules:

```bash
#!/usr/bin/env bash
set -euo pipefail
exec env PYTHONPATH="$SCRIPT_DIR" python3 -m research_store.cli "$@"
```

### 14.3 Never invoke scripts directly

Use `rtk proxy` at the outer agent-visible boundary. Never add RTK inside wrappers — their subprocesses must retain unmodified streams and exit codes.

### 14.4 Idempotency

Every state mutation must be idempotent:

```python
# Same idempotency key + same revision = returns original
# Conflicting reuse = raises ConflictError
```

### 14.5 Fail visibly

Degraded retrieval, model failure, alias mismatch, extraction fallback, incomplete coverage, and audit failure must be surfaced in state and output. They must not silently become normal success.

### 14.6 Version everything

Schemas, prompts, policy rules, embedding definitions, chunker versions — all versioned. Never silently change behavior without a version bump.

### 14.7 Preserve Phase 1–6 invariants

Unless explicitly retired by an assigned issue, do not:

- Change authority boundaries.
- Remove legacy production paths without benchmark approval.
- Introduce a second report, cache, lease, or resource authority.
- Bypass EvidencePacket validation.
- Invent citations or silently remove unsupported claims.
- Repeat completed semantic stages unnecessarily.
- Allow caches to bypass schema or reference validation.
- Batch jobs with incompatible output-affecting configuration.
- Hide endpoint failures or resource exhaustion.

---

## 15. Testing guidance

### 15.1 Deterministic unit tests

Run without network:

```bash
env PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider \
  "<skill-root>/scripts/test_classifier.py" \
  "<skill-root>/scripts/test_workflow.py" \
  "<skill-root>/scripts/test_budget_policy.py" \
  "<skill-root>/scripts/test_research_store.py" \
  "<skill-root>/scripts/test_index_runtime.py"
```

### 15.2 Integration tests

Require disposable PostgreSQL:

```bash
export RESEARCH_STORE_TEST_ALLOW_RESET='<db-name>'
export DATABASE_URL='postgresql://research:...@localhost/<db-name>'
pytest -q -p no:cacheprovider "<skill-root>/scripts/test_research_store_integration.py"
```

### 15.3 What to test for every change

1. **Bounded agent handoff** — semantic proposals don't mutate state directly.
2. **Citation resolution** — every citation resolves to a passage.
3. **Local synthesis stage failure and resume** — failed stages are retryable.
4. **Local endpoint outage** — failures are recorded, not swallowed.
5. **Structured-output failure** — invalid JSON is queryable, not lost.
6. **Invented citation** — rejected by validation.
7. **Unsupported claim** — marked explicitly.
8. **Stale packet revision** — synthesis stage fails, not proceeds.
9. **Cache key identity** — identical inputs produce identical keys.
10. **Cache invalidation** — changed policy invalidates entries.
11. **Stale cached references** — revalidated against current schema.
12. **Cache loss** — no workflow state lost.
13. **Partial embedding-batch failure** — successful jobs committed, failed jobs retried.
14. **Lease expiry** — expired leases are reclaimed.
15. **Vector-dimension mismatch** — dense retrieval falls back to lexical.
16. **Idempotent Qdrant replay** — duplicate upserts are safe.
17. **Concurrency caps** — backpressure is enforced.
18. **Endpoint restart** — in-flight requests fail gracefully.
19. **Backpressure** — queued requests are tracked.
20. **Resource exhaustion** — resource-limited errors are explicit.
21. **Benchmark reproducibility** — fixed dataset produces consistent results.
22. **Compatibility-only legacy paths** — shadow mode does not mutate state.
23. **Recovery documentation commands** — all CLI commands are documented and runnable.

> **Note:** Items 22–23 are verified by separate test suites, not by the generic
> unit test suite in Section 15.1: item 22 is covered by
> `test_workflow.py` (legacy adapter compatibility tests) and item 23 is covered
> by `test_documentation.py` (documentation command verification).

### 15.4 Migration tests

For every migration:

1. **Fresh migration** — new database, all migrations apply.
2. **Upgrade from current head** — existing database upgrades cleanly.
3. **Populated database** — corpus data is preserved.
4. **Constraints and indexes** — all new constraints fire correctly.
5. **Phase 1–6 data preservation** — no existing data is modified.
6. **Forward repair** — if needed, a forward-repair migration is tested.

---

_Cross-reference `operations-runbook.md` for operational procedures, `migration-guide.md` for migration procedures, `research-store-architecture.md` for authority boundaries, `workflow-state-schema.md` for the state machine, and `budget-policy.md` for budget details._
