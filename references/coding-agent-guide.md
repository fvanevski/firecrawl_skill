<!-- @format -->

# Coding Agent Guide

Authoritative implementation guide for agents modifying the Firecrawl Research Skill.

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

## 1. Architecture overview

```text
Host agent or user
  -> scripts/fresearch public controller
     -> persisted semantic ResearchSpec/SearchPlan authority
     -> retained-corpus review
     -> PostgreSQL-authoritative acquisition when needed
        -> Firecrawl provider transport after preflight
        -> PostgreSQL workflow, provenance, identities, evidence, and jobs
        -> BLOB_ROOT immutable provider bytes
        -> Qdrant rebuildable projection
        -> Valkey optional wakeups
     -> bounded terminal result / host handoff
```

Primary entry points:

| Entry point | Role |
|---|---|
| `scripts/fresearch` | canonical normal-agent controller and typed result/action surface |
| `scripts/fsearch_smart` | deprecated exact compatibility delegate to `fresearch run`; no independent policy |
| `scripts/fsearch` | specialist authoritative search plus optional bounded extraction |
| `scripts/fscrape` | specialist authoritative direct URL extraction |
| `scripts/finspect` | database-native history, replay, candidate selection, attempts, and bounded inspection |
| `scripts/frun` | specialist run lifecycle operations |
| `scripts/research-db` | schema, retrieval, projection, export, and diagnostics |
| `scripts/live_validate.py` | bounded live authority and recovery validation |

## 2. Authority boundaries

### Target A

- PostgreSQL owns workflow, invocation, provenance, corpus identity, evidence, audit, and job truth.
- `BLOB_ROOT` owns immutable provider payload bytes referenced by digest.
- Qdrant is a replaceable projection.
- Valkey is optional coordination.
- Ephemeral files are never runtime authority.

Do not migrate payload bytes into PostgreSQL, add a second workflow store, or treat a presentation export as an input without an explicit future-target issue.

### Deterministic authority

Deterministic code owns identity allocation, schema validation, content hashing, state transitions, budget enforcement, transaction boundaries, index activation, and failure ordering.

Semantic systems propose research scope, query plans, relevance, coverage, evidence roles, and drafts. Their outputs must be versioned, schema-validated, reference-validated, and committed through deterministic services.

### Fail-closed acquisition

The preflight must validate configuration, schema head, writable privileges, `BLOB_ROOT`, and run eligibility before constructing or invoking Firecrawl. Tests must prove the provider adapter and network log remain untouched on every preflight failure.

## 3. Execution modes

- `agent_led`: host supplies semantic decisions.
- `autonomous_local`: configured local model supplies semantic decisions.
- `deterministic_debug`: fixtures supply semantic decisions.

Persistence rules do not vary by mode. The current public `fresearch` surface has no standalone dry-run/spec-skeleton workflow; deterministic-debug semantics are exercised through the same versioned controller/service contracts rather than a second preview controller.

Mode changes are append-only compare-and-swap operations and invalidate affected semantic artifacts without deleting provenance.

## 4. Budget policy

`BudgetPolicy` maps validated semantic scope to versioned hard caps. Objective length is not a policy input.

| Tier | Search branches | Results per branch | Extraction attempts | Success target |
|---|---:|---:|---:|---:|
| focused | 2 | 15 | 8 | 6 |
| standard | 3 | 25 | 18 | 12 |
| intensive | 5 | 40 | 36 | 25 |

User limits may tighten but not exceed policy. Persist immutable budget snapshots. Search calls, extraction attempts, successes, retries, and live-validation provider processes consume their documented budgets.

## 5. Coverage-led research

Normal progression is controller-owned: semantic scope is validated and persisted, retained evidence is evaluated first, and provider acquisition opens only when authoritative coverage requires it. The host follows typed `fresearch` dispositions and durable `oa_<uuid>` actions rather than constructing lifecycle commands.

Create coverage items before acquisition. Every additional query or extraction must identify:

1. the unresolved gap;
2. expected contribution;
3. resource cost;
4. proposing semantic authority;
5. deterministic authorization.

Stop based on coverage, not page count, word count, or iteration count. Failed extraction produces explicit attempt state and a bounded pivot, not silent replacement.

## 6. State machine

```text
created -> planning -> corpus_review -> acquiring -> extracting -> indexing
-> coverage_review -> retrieving -> synthesizing -> validating
-> completed | partial | failed
```

Cancellation is explicit from nonterminal states. All mutations use lifecycle revision and idempotency keys. Terminal runs require explicit reopen.

Wrapper code may advance only the states its operation reaches. It must not fake completion or infer state from non-authoritative layers.

## 7. Extraction and derivations

```text
provider bytes
  -> immutable snapshot digest
  -> versioned normalized document
  -> blocks
  -> chunks
  -> embedding manifest and PostgreSQL job
  -> fingerprinted Qdrant point
```

Use `rederive` for parser, normalizer, tokenizer, or chunker upgrades. Never manufacture a new snapshot for unchanged provider bytes.

Structured extraction must validate against the requested schema before successful document ingestion. Preserve invalid payload provenance as a failed attempt.

Explicit exports:

```bash
rtk proxy "<skill-root>/scripts/research-db" export-invocation "fc_<uuid>" --output invocation.json
rtk proxy "<skill-root>/scripts/research-db" export-run "fr_<uuid>" --output run.json
```

Exports are not runtime inputs.

## 8. Retrieval and evidence

Retrieval combines PostgreSQL lexical candidates, compatible Qdrant dense candidates, reciprocal-rank fusion, and local reranking. On fingerprint mismatch, skip dense query embedding, remain lexical, and report unhealthy projection state.

```bash
rtk proxy "<skill-root>/scripts/research-db" search-assets "<query>" --limit 20
rtk proxy "<skill-root>/scripts/research-db" inspect-asset "<candidate-id>"
rtk proxy "<skill-root>/scripts/research-db" fetch-passages "<candidate-id>" --max-tokens 2000
```

History and retained provider replay use `finspect`. Candidate selection uses stable candidate UUIDs. Bounded outputs enforce record, byte, character, and tokenizer limits with scope-bound opaque cursors.

Evidence packets are the exclusive report evidence input. Every claim must resolve to packet passages. Unsupported, contradictory, qualifying, stale, or insufficient evidence remains explicit.

## 9. Final delivery, synthesis, and validation

Default `host_handoff` delivery crosses the internal synthesis lifecycle boundary without generating redundant full-prose draft/citation artifacts. Terminal completion instead binds the exact sealed membership and validated EvidencePacket to deterministic host-handoff provenance, and the public result exposes a bounded citation-ready handoff.

Explicit `self_synthesized` delivery retains the autonomous outline/binding/draft/citation/validation pipeline. Those semantic stages remain independently retryable; stale packet revision, invented citations, or missing bindings fail closed.

Neither delivery mode relaxes coverage, temporal qualification, evidence membership, or terminal completion authority.

## 10. Cache behavior

Semantic cache entries bind model, revision, prompt, schema, input hash, policy version, and reference hash. Cache loss cannot lose workflow state. Cache hits must be revalidated against current schema, policy, and evidence packet.

Do not let cache data advance state or bypass transport/provenance recording.

## 11. Embedding microbatching

Workers:

1. claim PostgreSQL jobs with `FOR UPDATE SKIP LOCKED`;
2. record lease token, owner, expiration, and attempt;
3. group only compatible index definitions;
4. idempotently upsert Qdrant;
5. complete exact manifests with current lease tokens.

Partial batch failure commits successful jobs and leaves failed jobs retryable. Expired final attempts become dead. Stale workers cannot overwrite reclaimed attempts.

## 12. Resource governance

Endpoint status, concurrency, queues, restarts, and resource limits remain explicit. No endpoint outage may silently switch to an unconfigured remote service.

```bash
rtk proxy "<skill-root>/scripts/research-db" endpoint-health
rtk proxy "<skill-root>/scripts/research-db" resource-status
rtk proxy "<skill-root>/scripts/research-db" doctor
```

Valkey loss must be tolerated through PostgreSQL polling. Qdrant loss must be recoverable through index rebuild.

## 13. Configuration variables

Normative definitions are in `operations-runbook.md`.

Key variables:

- `DATABASE_URL`
- `BLOB_ROOT`
- `QDRANT_URL`, `QDRANT_API_KEY`, `QDRANT_ALIAS`
- `VALKEY_URL`
- `EMBEDDING_URL`, `EMBEDDING_MODEL`, `EMBEDDING_REVISION`, `EMBEDDING_DIMENSION`
- `RERANKER_URL`, `RERANKER_MODEL`
- `FIRECRAWL_API_URL`, `FIRECRAWL_API_KEY`
- `FIRECRAWL_RESEARCH_RUN_ID`, `FIRECRAWL_INVOCATION_ID`
- `FIRECRAWL_LLM_LOCAL_BASE_URL`, `FIRECRAWL_AUDIT_AUTO_SEMANTIC`

Never print resolved secrets.

## 14. Coding conventions

- Keep shell launchers thin and typed Python services authoritative.
- Use `rtk proxy` only at the outer agent-visible boundary.
- Add type annotations to public functions.
- Preserve stable versioned schemas and bounded public output.
- Use canonical JSON and deterministic ordering where output is compared or exported.
- Reuse idempotency keys only for identical retries; reject conflicts.
- Surface partial and failed stages with nonzero exit status.
- Keep Target A boundaries unless the assigned issue explicitly changes them.
- Avoid adjacent cleanup, formatting churn, or speculative redesign.

## 15. Testing guidance

Every acquisition change requires focused tests for:

- invalid/missing database, schema, privileges, blob root, and run;
- zero provider/network activity after failed preflight;
- idempotent replay and conflicting-key rejection;
- provider response crash windows before and after commit;
- item-level partial failure;
- stable response and candidate selection;
- structured extraction validation;
- bounded inspection and cursor scope;
- blob integrity;
- worker lease expiry and restart;
- Valkey notification loss;
- Qdrant deletion, rebuild, activation, and rollback;
- explicit export reproducibility;
- monitored temporary storage purity.

Run:

```bash
ruff check .
ruff format --check .
env PYTHONDONTWRITEBYTECODE=1 \
  pytest -q -p no:cacheprovider tests/
```

Integration tests must target an explicitly disposable PostgreSQL database and the exact configured Qdrant and Valkey fixtures. Live validation must remain operation-capped and record exact candidate SHA.
