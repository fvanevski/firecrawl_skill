<!-- @format -->

## Product Requirements Document

## Firecrawl Research Skill Priority Architecture Refactor

**Document status:** Approved for implementation planning  
**Target repository:** `fvanevski/firecrawl_skill`  
**Primary audience:** Coding AI agents, maintainers, reviewers, and test agents  
**Document version:** 1.0  
**Implementation posture:** Incremental replacement with migration gates; no uncontrolled rewrite  
**Validation status:** Static architecture and code review completed. Runtime performance, model quality, and migration throughput remain **UNVERIFIED** until the test campaigns defined in this PRD are completed.

## 1\. Governing Architectural Decisions

The following decisions are approved and constitute the project’s architectural source of truth.

## GSP-A1 — PostgreSQL is the sole authoritative workflow state

PostgreSQL shall be authoritative for:

- research-run lifecycle;
- invocation lifecycle;
- search plans and revisions;
- raw search-response manifests;
- candidate metadata;
- candidate assessments;
- extraction attempts;
- corpus assets and derivations;
- coverage state;
- retrieval events;
- claim-to-evidence bindings;
- report artifacts;
- semantic audits;
- LLM call provenance;
- operational events.

Qdrant shall remain a rebuildable vector-retrieval projection.

Valkey shall remain a transient notification, coordination, and bounded-cache layer.

Content-addressed blob storage shall remain authoritative for immutable large payload bytes referenced from PostgreSQL.

Scratch directories and Catalog v5 files shall become compatibility, debugging, and export artifacts derived from authoritative state. They shall not independently determine workflow state.

## GSP-A2 — The workflow supports explicit execution modes

The public workflow shall support:

1.  `agent_led`
2.  `autonomous_local`

A third internal mode, `deterministic_debug`, shall be retained for testing, regression isolation, and infrastructure diagnosis, but shall not be represented as equivalent-quality autonomous research.

Execution mode shall be explicit, persisted, and immutable for an invocation unless a recorded strategy revision changes it.

## GSP-A3 — Adaptive research is coverage-led

Search, scraping, retrieval, and stopping decisions shall be based on the state of a persisted coverage ledger.

Successful page count, candidate count, word count, or elapsed iterations may contribute operational information but shall not independently determine research completion.

Every additional acquisition or retrieval action must identify:

- the unresolved coverage gap it addresses;
- the expected contribution;
- the hard budget it consumes;
- the decision authority that proposed it;
- the deterministic policy checks that authorized it.

## 2\. Executive Summary

The existing skill has evolved from a scratch-file-backed Firecrawl wrapper into a hybrid research platform comprising:

- broad metadata-first acquisition;
- LLM-generated research briefs and query plans;
- LLM candidate triage;
- iterative extraction;
- PostgreSQL corpus persistence;
- content-addressed blobs;
- Qdrant dense retrieval;
- PostgreSQL full-text retrieval;
- cross-encoder reranking;
- Valkey-backed worker wakeups;
- filesystem Catalog v5 auditing.

The refactoring has produced a strong deterministic corpus and indexing subsystem, but workflow authority and semantic control are fragmented.

The current architecture already defines PostgreSQL as authoritative for corpus assets, immutable snapshots, derivations, chunks, indexing manifests, and durable jobs, while Qdrant is rebuildable and Valkey is transient. The index lifecycle, model fingerprints, lease semantics, and recovery boundaries are well specified and should be preserved.

The remaining problems are concentrated in the research workflow:

- complexity and profile selection still rely on legacy keyword heuristics;
- search candidates that are not scraped are not first-class durable research assets;
- workflow state is divided between PostgreSQL and filesystem Catalog records;
- search recovery behavior is inconsistent with documented policy;
- adaptive stopping uses successful-page counts rather than evidence coverage;
- extraction quality is judged with weak proxies;
- evidence-packet construction is incomplete;
- final synthesis contracts differ implicitly depending on the host agent;
- semantic and operational traces are not uniformly queryable from PostgreSQL.

This project will replace those fragmented routines with a single persisted research state machine. Deterministic code will own identity, state, policy, validation, transactions, resource limits, provenance, and reproducibility. Generative models or the outer agent will own semantic interpretation, candidate relevance, evidence-role classification, coverage analysis, and synthesis through versioned structured contracts.

## 3\. Product Vision

The skill shall function as a durable, auditable research engine that can be operated by either:

- a foundational host model such as ChatGPT or Gemini; or
- a capable local model running on a single NVIDIA RTX 5090 32 GB system.

The engine shall:

1.  acquire a broad live candidate corpus before committing substantial agent context;
2.  persist the entire acquisition trail;
3.  selectively extract and index promising sources;
4.  continually evaluate unresolved research coverage;
5.  dynamically revise search strategy only in response to specific gaps;
6.  retrieve bounded, citation-ready passages;
7.  generate or support generation of a responsive report;
8.  retain complete provenance for debugging, regression analysis, and downstream audit.

The workflow shall be model-flexible but model-governed: models may make semantic judgments, but they may not mutate authoritative state without deterministic validation.

## 4\. Goals

## 4.1 Primary goals

### G-1 — Unify workflow authority

All authoritative research state must be stored transactionally in PostgreSQL.

### G-2 — Make execution mode explicit

The engine must behave predictably when driven by an outer agent and when operating autonomously with a local LLM.

### G-3 — Persist front-loaded acquisition

Every search query, response, candidate, branch occurrence, and candidate decision must survive independently of whether the candidate is scraped.

### G-4 — Replace page-count completion with coverage-led control

The engine must know which questions, claims, source classes, temporal requirements, and corroboration requirements are satisfied or unresolved.

### G-5 — Preserve deterministic guarantees

The existing strengths of immutable snapshots, content hashes, derivation versioning, transactional indexing jobs, lease safety, and rebuildable Qdrant projections must not regress.

### G-6 — Produce implementation-grade traceability

Every semantic or operational decision must identify:

- its inputs;
- its implementation version;
- its model or deterministic policy;
- its output;
- its validation status;
- its relationship to the research run.

### G-7 — Support local hardware efficiently

The autonomous-local workflow must be usable with a single 32 GB GPU, 32 GB system RAM, and the existing Linux environment without requiring a cloud fallback.

### G-8 — Enable measurable refactoring

The project must provide baseline, migration, regression, and comparative evaluation harnesses.

## 5\. Non-Goals

The following are outside the initial priority refactor:

1.  Replacing Firecrawl as the primary public acquisition backend.
2.  Building a general-purpose browser automation framework.
3.  Providing a consumer-facing graphical interface.
4.  Adding unsupported cloud-provider fallbacks automatically.
5.  Making Qdrant authoritative for source content or workflow state.
6.  Making Valkey durable.
7.  Replacing canonical source text with LLM summaries.
8.  Guaranteeing that a local model matches frontier-model research quality.
9.  Building a full knowledge-graph reasoning system before claim/evidence storage is stable.
10. Optimizing every workflow for maximum throughput before correctness and traceability are validated.

11. Adding pgvector merely because PostgreSQL supports it. It may be added only for a documented retrieval, fallback, evaluation, or transactional-embedding requirement.

## 6\. Current-State Baseline

This section records the current behavior that the implementation must replace or preserve.

## 6.1 Capabilities to preserve

The current model gateway correctly separates transport and response normalization from research judgment, validates structured output, records prompt hashes and provenance, and requires explicit commercial-provider configuration.

The current retrieval service combines PostgreSQL full-text search, Qdrant dense retrieval, reciprocal-rank fusion, and optional reranking.

The current persistence adapter preserves raw and normalized bytes separately and persists successful scrape results through a manifest rather than scanning directories.

The current staged audit architecture separates acquisition, evidence, and synthesis judgments and validates model-emitted evidence references against known packet identifiers.

## 6.2 Capabilities to replace

Automatic complexity classification currently relies on request length, word count, and a legacy set of technical keywords.

Search-recovery behavior may deterministically broaden an LLM-planned query despite comments indicating that this should not occur.

Research extraction terminates in part based on a derived number of successful URLs rather than question and claim coverage.

Only attempted scrape results are sent to the PostgreSQL persistence adapter; the complete unscripted candidate ledger is not persisted as first-class corpus metadata.

The current evidence packet contains empty corroborating, contradicting, and near-duplicate groups rather than constructing them.

The current structural parser and chunker use regular-expression block detection, fixed character limits, and approximate token counts.

## 7\. Users and Operating Contexts

## 7.1 Foundational host-agent operator

Examples:

- OpenAI ChatGPT;
- Google Gemini;
- another capable hosted agent with tool-use capability.

Expected behavior:

- the outer agent interprets the user’s intent;
- the outer agent may approve, revise, or supply the `ResearchSpec`;
- the skill performs acquisition, persistence, indexing, retrieval, and trace capture;
- the outer agent selects or requests evidence expansions;
- the outer agent generates the final report unless autonomous synthesis is explicitly requested.

## 7.2 Autonomous local operator

Expected local environment:

- Garuda Linux;
- NVIDIA RTX 5090 with 32 GB VRAM;
- 32 GB system RAM;
- AMD Ryzen 9 5950X;
- OpenAI-compatible local model endpoint;
- local embedding endpoint;
- local reranker endpoint;
- PostgreSQL;
- Qdrant;
- Valkey.

Expected behavior:

- the skill invokes the local LLM for structured semantic stages;
- code enforces all state changes and hard limits;
- large semantic tasks are decomposed into bounded calls;
- no commercial fallback occurs without explicit configuration;
- the workflow remains resumable after model or service failure.

## 7.3 Maintainer or debugging operator

The operator needs:

- deterministic reproduction;
- complete event history;
- prompt and policy version comparison;
- regression fixtures;
- replay without issuing new live searches where retained inputs are sufficient;
- clear distinction between mechanical failure and semantic failure.

## 8\. Design Principles

## DP-1 — Deterministic authority

Only deterministic code may:

- allocate identifiers;
- commit workflow transitions;
- enforce budgets;
- validate schemas;
- write authoritative state;
- calculate content hashes;
- determine transaction boundaries;
- declare persistence success;
- activate or retire indexes;
- verify evidence references.

## DP-2 — Semantic delegation

The outer agent or configured LLM may:

- interpret the objective;
- propose research questions and claims;
- propose search queries;
- assess candidate relevance;
- assess source role;
- map passages to claims;
- identify contradiction or qualification;
- assess unresolved coverage;
- draft reports;
- perform semantic audits.

## DP-3 — Proposal before mutation

A semantic component produces a structured proposal. A deterministic policy engine validates the proposal before an action is scheduled or authoritative state is changed.

## DP-4 — Immutable evidence lineage

Raw source bytes, normalized documents, structural blocks, chunks, semantic annotations, and reports must remain distinguishable.

## DP-5 — Fail visibly

Degraded retrieval, model failure, alias mismatch, extraction fallback, incomplete coverage, and audit failure must be surfaced in state and output. They must not silently become normal success.

## DP-6 — Bounded context

Agents and LLMs receive compact manifests first and full passages only through explicit bounded expansion.

## DP-7 — Replayability

A research run must be reproducible from retained plans, raw search responses, candidate records, source snapshots, prompts, policies, and model provenance to the maximum extent possible.

## DP-8 — Version all semantic behavior

Research schemas, prompts, policy rules, extraction rules, parser versions, chunker versions, embedding definitions, reranker definitions, and audit templates must be versioned.

## 9\. Target System Architecture

```sql
User or host agent
        |
        v
Research API / CLI
        |
        v
Research Orchestrator
        |
        +--> PostgreSQL Research State
        |      - runs
        |      - invocations
        |      - plans
        |      - candidates
        |      - assessments
        |      - coverage
        |      - events
        |      - claims
        |      - reports
        |      - audits
        |
        +--> Acquisition Adapter
        |      - Firecrawl search
        |      - Firecrawl scrape
        |      - deterministic fallback extractors
        |
        +--> Blob Store
        |      - raw search responses
        |      - raw source bytes
        |      - report payloads
        |
        +--> Corpus Service
        |      - canonical sources
        |      - immutable snapshots
        |      - normalized documents
        |      - blocks
        |      - chunks
        |
        +--> Index Outbox / Worker
        |      - embedding service
        |      - Qdrant projection
        |      - Valkey wakeups
        |
        +--> Retrieval Service
        |      - PostgreSQL FTS
        |      - Qdrant dense search
        |      - RRF
        |      - reranker
        |
        +--> Semantic Decision Gateway
        |      - host-agent proposals
        |      - local LLM proposals
        |      - deterministic-debug policies
        |
        +--> Evidence Builder
        |      - claim bindings
        |      - corroboration
        |      - contradiction
        |      - near-duplicate handling
        |
        +--> Report / Audit Services
               - agent-led export
               - autonomous-local synthesis
               - staged semantic audit
```

## 10\. Research State Machine

## 10.1 Run states

The authoritative `research_runs.state` field shall support:

- `created`
- `planning`
- `corpus_review`
- `acquiring`
- `extracting`
- `indexing`
- `coverage_review`
- `retrieving`
- `synthesizing`
- `validating`
- `completed`
- `partial`
- `failed`
- `cancelled`

A run may also carry a lifecycle revision number and a `reopened_from_revision` reference.

## 10.2 Permitted transitions

```rust
created -> planning
planning -> corpus_review
planning -> failed

corpus_review -> acquiring
corpus_review -> retrieving
corpus_review -> failed

acquiring -> coverage_review
acquiring -> extracting
acquiring -> failed

extracting -> indexing
extracting -> coverage_review
extracting -> failed

indexing -> coverage_review
indexing -> partial
indexing -> failed

coverage_review -> acquiring
coverage_review -> extracting
coverage_review -> retrieving
coverage_review -> synthesizing
coverage_review -> partial
coverage_review -> failed

retrieving -> coverage_review
retrieving -> synthesizing
retrieving -> failed

synthesizing -> validating
synthesizing -> failed

validating -> completed
validating -> partial
validating -> failed
```

Transitions not listed above must be rejected unless a migration or explicit administrative repair operation is being performed.

## 10.3 Transition record

Every transition shall record:

- transition ID;
- run ID;
- prior state;
- next state;
- triggering event;
- actor type;
- actor identifier;
- policy version;
- semantic proposal ID, where applicable;
- validation result;
- timestamp;
- idempotency key;
- error, where applicable.

## 10.4 State mutation rules

1.  State transitions occur within PostgreSQL transactions.
2.  External side effects use transactional outbox records.
3.  Retried commands use idempotency keys.
4.  The current state must be locked before transition.
5.  A stale semantic proposal may not mutate a newer run revision.
6.  Terminal runs may not receive new acquisition, extraction, or retrieval work without reopening.

## 11\. Execution Modes

## 11.1 `agent_led`

### Responsibilities of the host agent

- approve or supply the `ResearchSpec`;
- make semantic decisions when requested;
- review coverage summaries;
- request retrieval expansion;
- produce the final report unless autonomous synthesis is explicitly invoked.

### Responsibilities of the skill

- validate proposals;
- persist all state;
- execute searches and scrapes;
- manage retries;
- build candidate manifests;
- maintain the coverage ledger;
- retrieve passages;
- construct a structured evidence packet;
- validate citations and provenance.

### Requirement

The skill must not invoke the configured inner generative LLM for a semantic decision when a valid host-agent decision for the same decision point has been supplied.

## 11.2 `autonomous_local`

### Responsibilities of the local LLM

- generate the initial `ResearchSpec`;
- propose the search plan;
- triage candidates;
- propose adaptive search revisions;
- assess coverage;
- map evidence to claims;
- draft the report;
- participate in semantic validation and auditing.

### Responsibilities of deterministic code

All responsibilities listed under DP-1 remain deterministic.

### Requirement

Each LLM stage must be independently retryable and resumable.

## 11.3 `deterministic_debug`

This mode shall:

- avoid generative semantic decisions;
- use explicit user-supplied plans or deterministic fixtures;
- execute acquisition, extraction, persistence, indexing, and retrieval;
- make no claim of semantic research adequacy;
- mark semantic coverage fields as `unassessed`.

## 12\. Core Domain Schemas

All schemas shall be maintained as versioned JSON Schema or typed Python models with generated JSON Schema.

## 12.1 `ResearchSpec`

Required fields:

```json
{
  "schema_version": "research-spec-v1",
  "objective": "string",
  "research_archetype": "string",
  "risk_level": "low|medium|high",
  "execution_mode": "agent_led|autonomous_local|deterministic_debug",
  "questions": [],
  "claims_to_validate": [],
  "entities": [],
  "jurisdictions": [],
  "time_window": {},
  "freshness_requirements": [],
  "required_source_classes": [],
  "corroboration_requirements": [],
  "contradiction_requirements": [],
  "excluded_interpretations": [],
  "structured_data_requirements": [],
  "completion_criteria": [],
  "user_constraints": [],
  "ambiguities": [],
  "assumptions": []
}
```

Each question and claim must have a stable ID.

## 12.2 `SearchPlan`

Required fields:

```json
{
  "schema_version": "search-plan-v1",
  "research_spec_id": "uuid",
  "revision": 1,
  "queries": [
    {
      "query_id": "uuid",
      "query": "string",
      "facet": "string",
      "target_question_ids": [],
      "target_claim_ids": [],
      "intended_source_classes": [],
      "expected_organizations": [],
      "freshness_requirement": {},
      "expected_contribution": "string",
      "domain_restrictions": [],
      "negative_terms": [],
      "priority": 0
    }
  ]
}
```

## 12.3 `CandidateAssessment`

Required fields:

```json
{
  "schema_version": "candidate-assessment-v1",
  "candidate_id": "uuid",
  "relevance": "high|medium|low|unrelated|uncertain",
  "source_role": "primary|controlling|authoritative_secondary|independent_secondary|context_only|unsuitable|uncertain",
  "target_question_ids": [],
  "target_claim_ids": [],
  "freshness_assessment": {},
  "independence_assessment": {},
  "extraction_recommendation": "scrape|metadata_only|defer|reject",
  "priority": 0,
  "rationale": "string",
  "confidence": 0.0,
  "uncertainty": "string"
}
```

## 12.4 `CoverageLedger`

The coverage ledger shall be derived from immutable coverage events but exposed as a current-state projection.

Required fields:

```json
{
  "schema_version": "coverage-ledger-v1",
  "run_id": "uuid",
  "revision": 1,
  "items": [
    {
      "coverage_item_id": "uuid",
      "item_type": "question|claim|source_requirement|freshness_requirement|corroboration_requirement|contradiction_requirement",
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

## 12.5 `StrategyRevisionProposal`

```json
{
  "schema_version": "strategy-revision-v1",
  "run_revision": 1,
  "decision": "search|scrape|retrieve|synthesize|stop_partial|stop_failed",
  "target_coverage_item_ids": [],
  "proposed_queries": [],
  "proposed_candidate_ids": [],
  "proposed_retrieval_queries": [],
  "expected_contribution": "string",
  "estimated_cost": {},
  "rationale": "string",
  "confidence": 0.0
}
```

## 12.6 `EvidencePacket`

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

## 13\. Functional Requirements

## FR-001 — PostgreSQL authoritative workflow model

The system shall add or extend PostgreSQL tables for:

- `research_runs`
- `research_run_transitions`
- `research_invocations`
- `research_events`
- `research_specs`
- `search_plans`
- `search_plan_queries`
- `search_responses`
- `search_candidates`
- `candidate_occurrences`
- `candidate_assessments`
- `extraction_attempts`
- `coverage_events`
- `coverage_snapshots`
- `strategy_revisions`
- `semantic_calls`
- `semantic_artifacts`
- `research_claims`
- `claim_evidence_links`
- `report_artifacts`
- `audit_assessments`
- `compatibility_exports`

### Acceptance criteria

1.  A complete run can be reconstructed without reading scratch or Catalog files.
2.  A run’s current lifecycle state has one authoritative answer.
3.  Filesystem exports can be deleted and regenerated from PostgreSQL and blob storage.
4.  A failed filesystem export does not roll back committed research state.
5.  A PostgreSQL failure cannot result in a run being reported as authoritatively completed.

## FR-002 — Explicit execution-mode selection

The run-start API and CLI shall require or resolve an execution mode.

Defaults:

- host-agent integration: `agent_led`;
- standalone local CLI: `autonomous_local`;
- tests: explicit mode.

Execution mode must appear in:

- `ResearchSpec`;
- run metadata;
- event stream;
- report provenance;
- audit packet.

### Acceptance criteria

1.  The same run may not silently alternate between host-agent and local semantic authorities.
2.  A strategy revision changing mode records who requested and approved the change.
3.  Inner-LLM calls are absent from agent-led stages where host-agent output has been supplied.

## FR-003 — Structured `ResearchSpec`

The workflow shall replace legacy keyword complexity and run-profile classification with `ResearchSpec`.

The semantic authority proposes the specification.

Deterministic validation shall verify:

- stable IDs;
- enum values;
- nonempty objective;
- referenced question and claim IDs;
- time-window syntax;
- no unsupported execution mode;
- no unbounded completion criteria.

A deterministic conservative fallback may preserve the exact objective but must mark unresolved semantic fields as `unassessed` or `uncertain`. It shall not silently create broad facets.

### Acceptance criteria

1.  No workflow budget is selected directly from word count or topic length.
2.  Legacy complexity output may be retained only as diagnostic metadata during migration.
3.  Every query can be traced to a question, claim, or source requirement.

## FR-004 — Deterministic budget policy

A versioned budget policy shall map the validated `ResearchSpec` to:

- maximum search branches;
- results per branch;
- maximum extraction attempts;
- maximum successful extractions;
- maximum adaptive cycles;
- maximum LLM calls;
- maximum input and output tokens;
- maximum retrieval candidates;
- maximum reranker candidates;
- maximum evidence-packet tokens;
- maximum wall-clock duration where appropriate.

Policy inputs may include:

- research archetype;
- risk level;
- number of questions and claims;
- freshness sensitivity;
- expected disagreement;
- required source classes;
- user-supplied limits.

### Acceptance criteria

1.  The semantic authority may propose actions but cannot exceed policy.
2.  Every rejected proposal records the violated rule.
3.  Budgets are persisted with a policy version.
4.  User-specified stricter limits override defaults.

## FR-005 — Retained-corpus preflight

Before issuing a live search, the system shall evaluate retained corpus coverage.

The preflight shall:

1.  retrieve candidate chunks from PostgreSQL FTS and Qdrant;
2.  rerank the bounded fused set;
3.  evaluate freshness against the `ResearchSpec`;
4.  map retained evidence to coverage items;
5.  determine whether new acquisition is required.

### Acceptance criteria

1.  Live search is skipped when retained evidence satisfies all applicable completion criteria.
2.  Skipped acquisition records the corpus state and coverage reasoning.
3.  Stale retained evidence may be used as background but cannot satisfy current-freshness requirements without an explicit waiver.

## FR-006 — Search-plan proposal and validation

Search queries shall be proposed by the host agent or local LLM.

Deterministic validation shall enforce:

- nonempty query;
- preserved distinctive entities;
- preserved jurisdiction and time constraints where required;
- query normalization;
- duplicate and near-duplicate rejection;
- search-backend syntax constraints;
- domain-restriction limits;
- budget limits;
- target coverage IDs;
- no unexplained scope expansion.

### Acceptance criteria

1.  Every executed query has a persisted plan record.
2.  Every query identifies its expected coverage contribution.
3.  Recovery queries are validated by the same policy as initial queries.
4.  Deterministic string broadening cannot execute without a recorded strategy-revision proposal.

## FR-007 — Complete candidate-corpus persistence

Every acquired search candidate shall be persisted before semantic triage or scraping.

Persisted candidate data shall include:

- query and response IDs;
- canonical URL;
- original URL;
- title;
- snippet;
- description;
- published-date signals;
- rank;
- search source;
- provider request ID;
- raw-response blob reference;
- branch occurrence;
- duplicate-group identity;
- acquisition timestamp;
- backend metadata;
- parsing warnings.

One logical candidate may have multiple occurrences across queries.

### Acceptance criteria

1.  An unscripted candidate remains queryable after scratch deletion.
2.  Candidate recurrence across branches is retained.
3.  Candidate triage may be rerun without issuing a new search.
4.  Search-response payloads are content-addressed or otherwise immutably referenced.
5.  Candidate identity and source identity remain distinct until canonical source ingestion.

## FR-008 — Multi-stage candidate ranking and triage

Candidate selection shall use three layers:

### Layer 1 — Deterministic preprocessing

- URL canonicalization;
- duplicate detection;
- invalid URL rejection;
- domain counts;
- branch recurrence;
- date-signal extraction;
- known source metadata;
- policy exclusions.

### Layer 2 — Learned retrieval scoring

Where configured:

- embedding similarity;
- cross-encoder reranking;
- source-type classifier;
- duplicate or similarity classifier.

### Layer 3 — Semantic assessment

The host agent or local LLM assesses:

- relevance;
- source role;
- question and claim coverage;
- freshness;
- independence;
- extraction priority;
- uncertainty.

### Acceptance criteria

1.  LLM triage is not required for candidates deterministically rejected as invalid.
2.  Learned prefiltering reduces but does not silently discard the durable candidate ledger.
3.  Every candidate selected for extraction has a recorded reason.
4.  Every rejected high-ranked candidate has either a deterministic rejection reason or semantic assessment.
5.  Triage coverage rate is reported.

## FR-009 — Extraction planning and attempt provenance

Extraction shall be modeled as explicit attempts, not as a single success field.

Each attempt shall record:

- candidate ID;
- extraction method;
- method version;
- requested format;
- start and end time;
- exit status;
- HTTP or backend status;
- raw payload blob;
- normalized payload;
- parser used;
- quality metrics;
- failure class;
- retry relationship;
- final disposition.

Recommended deterministic extraction order:

1.  Firecrawl main-content extraction;
2.  Firecrawl full-page extraction;
3.  document-type-specific deterministic extractor;
4.  browser-capable or alternate acquisition adapter where configured;
5.  structured extraction where required;
6.  semantic quality adjudication for ambiguous cases.

Regex-only HTML-to-text conversion shall not remain the preferred fallback.

### Acceptance criteria

1.  A source may have multiple extraction attempts.
2.  Raw output from every materially different attempt is retained or hashed.
3.  Extraction success is not determined solely by word count.
4.  The final normalized document identifies the attempt from which it was derived.
5.  Anti-bot, timeout, empty-content, parser, and schema failures are distinguishable.

## FR-010 — Extraction-quality model

The deterministic quality evaluator shall calculate, where applicable:

- byte length;
- visible-text length;
- paragraph count;
- heading count;
- list and table count;
- link density;
- boilerplate ratio;
- title presence;
- language confidence;
- content-type consistency;
- anti-bot markers;
- duplicate-content similarity;
- query-term coverage;
- required structured-field coverage;
- parser warnings;
- code-to-prose ratio;
- extraction-method confidence.

The result shall be:

- `acceptable`;
- `poor`;
- `ambiguous`.

Only `ambiguous` results should require semantic adjudication by default.

### Acceptance criteria

1.  Short official notices can be accepted when structure and relevance are adequate.
2.  Long anti-bot pages can be rejected despite high word count.
3.  Quality metrics and final disposition are persisted separately.

## FR-011 — Canonical parsing and chunking

Canonical text parsing shall remain deterministic.

The parser subsystem shall support typed parsers or adapters for:

- Markdown;
- HTML;
- JSON;
- plain text;
- source code;
- PDF-derived text where available;
- legal or legislative hierarchical text where detected.

The initial implementation may prioritize Markdown, HTML, JSON, and plain text.

Chunking shall:

- use an actual tokenizer compatible with the embedding or retrieval configuration;
- prevent oversized single blocks;
- preserve heading hierarchy;
- preserve block offsets;
- preserve table and code boundaries where possible;
- support parent-child retrieval;
- version every parser, normalizer, and chunker.

### Acceptance criteria

1.  Re-parsing does not create a false new source snapshot.
2.  Parser and chunker upgrades create new derivations.
3.  Chunk IDs remain stable for identical derivation inputs and versions.
4.  Every passage can be traced to source offsets or structural blocks.

## FR-012 — Coverage-led adaptive control

After every significant acquisition, extraction, indexing, or retrieval cycle, the system shall update the coverage ledger.

The semantic authority shall propose the next action based on unresolved coverage.

The deterministic policy engine shall authorize one of:

- search;
- scrape;
- retrieve;
- synthesize;
- stop partial;
- stop failed.

### Required stop rules

The system shall stop and proceed to synthesis when:

1.  all mandatory coverage items are satisfied; or
2.  remaining items are waived by the user or policy; or
3.  hard budgets are exhausted and partial reporting is permitted.

The system shall stop as failed when:

1.  required evidence is unavailable;
2.  acquisition and extraction cannot produce usable evidence;
3.  the semantic authority cannot produce valid proposals within retry limits;
4.  persistence or workflow authority is unavailable;
5.  policy prohibits proceeding.

### Acceptance criteria

1.  Successful URL count cannot independently trigger completion.
2.  Every additional search branch identifies targeted coverage items.
3.  The ledger records support, contradiction, qualification, and blocked gaps.
4.  Iteration history can be replayed from coverage snapshots and strategy revisions.

## FR-013 — Retrieval transparency

Retrieval results shall expose:

- requested retrieval mode;
- executed retrieval mode;
- active index fingerprint;
- lexical candidate count;
- semantic candidate count;
- fused candidate count;
- reranked candidate count;
- degraded components;
- component errors;
- filters applied;
- selected and rejected candidates;
- stage-specific scores;
- stage-specific ranks.

Semantic retrieval failures shall not be silently converted into ordinary lexical-only success.

### Acceptance criteria

1.  A Qdrant outage is visible in the result and event log.
2.  Alias or fingerprint mismatch is visible.
3.  Every selected evidence passage can be traced through retrieval stages.
4.  Rejected candidates may be logged with rejection reasons.
5.  Reranker unavailability produces a declared degraded mode.

## FR-014 — Qdrant sparse-vector decision

The implementation shall make an explicit choice:

### Option A

Implement sparse-vector generation, storage, and query behavior with tests demonstrating value.

### Option B

Remove unused sparse-vector collection configuration and document PostgreSQL FTS as the lexical retriever.

Default implementation decision: **Option B**, unless an evaluation issue demonstrates a clear reason to retain Qdrant sparse vectors.

### Acceptance criteria

No production collection shall declare an unused vector configuration without a documented planned consumer.

## FR-015 — Evidence-packet construction

The evidence builder shall produce a complete `EvidencePacket`.

Deterministic responsibilities:

- passage provenance;
- token budgeting;
- source version checks;
- exact IDs;
- duplicate grouping;
- temporal ordering;
- domain diversity;
- citation formatting;
- evidence-reference validation.

Semantic responsibilities:

- claim-to-passage mapping;
- support, contradiction, qualification, and context labels;
- source-role assessment;
- unresolved uncertainty;
- concise evidence summaries;
- suggested additional retrieval where necessary.

### Acceptance criteria

1.  Corroborating and contradicting groups are populated when evidence exists.
2.  Empty groups indicate evaluated absence, not unimplemented behavior.
3.  Every claim in the report plan has evidence or is marked unsupported.
4.  Near-duplicate sources do not inflate independent corroboration counts.
5.  Source independence reasoning is traceable.

## FR-016 — Dual report-synthesis contract

### Agent-led output

The skill shall return:

- the validated `ResearchSpec`;
- current coverage ledger;
- evidence packet;
- report outline proposal, where requested;
- citation-ready passage records;
- explicit limitations;
- unresolved claims.

### Autonomous-local output

The local synthesis pipeline shall:

1.  generate a claim outline;
2.  bind each claim to passage IDs;
3.  draft the report;
4.  run a citation and entailment consistency pass;
5.  run a limitation and uncertainty pass;
6.  persist the report and claim manifest;
7.  return the final report with provenance.

### Acceptance criteria

1.  Report generation cannot cite unknown passage IDs.
2.  Unsupported claims are rejected or explicitly labeled.
3.  The final report stores its exact evidence-packet revision.
4.  The agent-led and autonomous-local modes use the same evidence schema.
5.  A report may be regenerated from the same evidence packet without repeating acquisition.

## FR-017 — Semantic audit persistence

Staged audits shall be stored in PostgreSQL and linked to:

- run revision;
- evidence-packet hash;
- report hash;
- evaluator version;
- prompt version;
- model fingerprint;
- stage set.

Equivalent current audits shall be reused rather than executed twice.

### Acceptance criteria

1.  Automatic audit scheduling is idempotent.
2.  Filesystem audit exports are derived from database records.
3.  Audit results become stale when their target evidence or report changes.
4.  Invented evidence references fail validation.
5.  Audit failure does not erase the completed operational trace.

## FR-018 — Compatibility exports

The system shall support deterministic generation of:

- `_meta.json`;
- `_context.json`;
- `_candidates.json`;
- `_evidence.json`;
- `_corpus.json`;
- Markdown indexes;
- Catalog v5-compatible records where migration requires them.

Each export shall include:

- authoritative run or invocation ID;
- database revision or event cursor;
- export schema version;
- generated timestamp;
- source-state hash.

### Acceptance criteria

1.  Exports can be regenerated.
2.  Export deletion does not remove authoritative state.
3.  Export failure is reported separately from workflow failure.
4.  No routine retrieval operation scans scratch directories.

## 14\. PostgreSQL Data Model Requirements

## 14.1 New or expanded tables

### `research_runs`

Key fields:

- `id`
- `external_run_id`
- `state`
- `lifecycle_revision`
- `execution_mode`
- `objective`
- `research_spec_id`
- `budget_policy_version`
- `current_coverage_revision`
- `declared_outcome`
- `started_at`
- `completed_at`
- `error`
- `metadata`

### `research_run_transitions`

Immutable transition ledger.

### `research_invocations`

Represents top-level or child operations such as search, scrape, retrieval, synthesis, and audit.

### `research_events`

Append-only event stream with event type, actor, payload, and run revision.

### `research_specs`

Versioned immutable specification records.

### `search_plans`

Versioned plan headers.

### `search_plan_queries`

One row per planned query.

### `search_responses`

Stores provider metadata and raw-response blob reference.

### `search_candidates`

Logical candidate metadata.

### `candidate_occurrences`

Maps candidates to search responses, queries, ranks, and branch metadata.

### `candidate_assessments`

Versioned semantic or deterministic assessments.

### `extraction_attempts`

One row per extraction method attempt.

### `coverage_events`

Append-only changes to coverage items.

### `coverage_snapshots`

Materialized or immutable snapshots of ledger state.

### `strategy_revisions`

Semantic proposal plus deterministic validation outcome.

### `semantic_calls`

Transport and model-level provenance.

### `semantic_artifacts`

Structured outputs linked to semantic calls.

### `research_claims`

Stable claim records.

### `claim_evidence_links`

Links claims to chunks, blocks, or passages with relationship labels.

### `report_artifacts`

Report text or blob reference, hash, evidence revision, and generation provenance.

### `audit_assessments`

Staged audit records.

## 14.2 Referential invariants

1.  Candidate occurrence must reference an existing query and response.
2.  Candidate assessment must reference a persisted candidate.
3.  Extraction attempt must reference a candidate.
4.  A corpus snapshot derived from extraction must reference the successful extraction attempt.
5.  Claim-evidence links must reference the exact source snapshot and passage derivation.
6.  Semantic artifacts must reference a semantic call.
7.  Strategy revisions must reference the coverage revision they evaluated.
8.  Report artifacts must reference an evidence packet and coverage revision.
9.  Audit assessments must reference an immutable target hash.
10. Run transitions must be append-only.

## 14.3 Transaction boundaries

The following must be atomic:

- run state transition plus transition record;
- search response plus all parsed candidates and occurrences;
- extraction-attempt completion plus normalized artifact registration;
- corpus-ingestion rows plus indexing outbox jobs;
- coverage update plus strategy-revision authorization;
- report registration plus claim manifest;
- audit registration plus audit-stage outputs.

Blob writes that precede rolled-back transactions shall be reportable orphans, not corpus records.

## 15\. Service Boundaries

The codebase shall be reorganized into explicit services.

## 15.1 `ResearchRunService`

Responsibilities:

- create, transition, reopen, complete, and fail runs;
- enforce lifecycle rules;
- manage idempotency;
- expose run status.

## 15.2 `ResearchPlanningService`

Responsibilities:

- accept host-agent or local-model proposals;
- validate `ResearchSpec`;
- validate search plans;
- invoke budget policy.

## 15.3 `AcquisitionService`

Responsibilities:

- execute search;
- persist raw response;
- parse and persist candidates;
- classify mechanical failures;
- execute approved search revisions.

## 15.4 `CandidateService`

Responsibilities:

- canonical candidate identity;
- occurrence aggregation;
- deterministic signals;
- learned scoring;
- semantic assessment storage.

## 15.5 `ExtractionService`

Responsibilities:

- plan and execute attempts;
- preserve raw output;
- evaluate quality;
- choose final normalized derivation;
- submit successful content to `CorpusService`.

## 15.6 `CoverageService`

Responsibilities:

- create coverage items from `ResearchSpec`;
- apply evidence and candidate events;
- calculate current projection;
- request semantic coverage assessment;
- validate next-action proposals.

## 15.7 `CorpusService`

Preserve current responsibilities for:

- canonical sources;
- immutable snapshots;
- documents;
- blocks;
- chunks;
- derivation versioning;
- transactional indexing jobs.

## 15.8 `RetrievalService`

Responsibilities:

- lexical retrieval;
- dense retrieval;
- fusion;
- reranking;
- degraded-mode reporting;
- retrieval-event logging;
- bounded passage expansion.

## 15.9 `EvidenceService`

Responsibilities:

- construct evidence packets;
- perform deterministic grouping;
- invoke semantic binding;
- validate references.

## 15.10 `ReportService`

Responsibilities:

- produce agent-led evidence exports;
- invoke autonomous-local synthesis;
- persist reports;
- validate claim bindings.

## 15.11 `AuditService`

Responsibilities:

- staged semantic audit;
- idempotent audit reuse;
- target-hash freshness;
- audit export.

## 15.12 `CompatibilityExportService`

Responsibilities:

- generate scratch and Catalog-compatible artifacts;
- never mutate authoritative state.

## 16\. CLI and API Surface

The existing wrappers may remain temporarily, but all new behavior shall route through the authoritative services.

Recommended CLI:

```php-template
research run start "<objective>" --mode agent-led
research run start "<objective>" --mode autonomous-local

research run status <run-id>
research run events <run-id>
research run reopen <run-id> --reason "<reason>"
research run cancel <run-id>

research spec propose <run-id> --llm local
research spec apply <run-id> --file research-spec.json

research plan propose <run-id>
research plan apply <run-id> --file search-plan.json

research acquire <run-id>
research candidates list <run-id>
research candidates assess <run-id> --llm local

research extract <run-id>
research coverage show <run-id>
research coverage evaluate <run-id>

research retrieve <run-id> "<query>"
research evidence build <run-id>
research report build <run-id> --mode autonomous-local
research report export <run-id>

research audit <run-id> --llm local
research export compatibility <run-id> --format catalog-v5
```

## 16.1 Machine-readable output

All commands shall support:

```css
--json
```

Machine-readable results must include:

- operation ID;
- run ID;
- prior and resulting state;
- authoritative record IDs;
- degraded components;
- warnings;
- next permitted actions.

## 16.2 Legacy wrapper mapping

During migration:

- `frun` delegates to `research run`;
- `fsearch_smart` delegates to planning, acquisition, coverage, and extraction services;
- `fsearch` delegates to a single-query acquisition operation;
- `fscrape` delegates to extraction operations;
- `research-db` remains the corpus and infrastructure administration interface until consolidated.

## 17\. Semantic Gateway Requirements

## 17.1 Supported semantic authorities

- host-agent supplied structured output;
- local OpenAI-compatible model;
- explicit OpenAI provider;
- explicit Gemini provider;
- deterministic-debug fixture.

## 17.2 Required provenance

Every semantic call shall record:

- semantic-call ID;
- run and invocation IDs;
- stage;
- provider;
- endpoint alias;
- requested model;
- returned model;
- model revision or fingerprint where available;
- prompt-template version;
- prompt hash;
- schema version;
- input artifact IDs;
- input token estimate;
- output token usage;
- latency;
- retry attempts;
- structured-output mode;
- validation errors;
- fallback use;
- final status.

## 17.3 Caching

Semantic outputs may be reused only when all of the following match:

- stage;
- prompt-template version;
- schema version;
- provider and model fingerprint;
- normalized input hash;
- applicable policy version.

Cached semantic outputs still require deterministic validation against current state.

## 18\. Local Resource Requirements

The autonomous-local workflow shall be designed for bounded execution on a single RTX 5090 system.

## 18.1 Required controls

- configurable maximum concurrent generative calls;
- configurable maximum model input tokens;
- candidate-card batching;
- bounded reranker candidate set;
- embedding microbatching;
- backpressure through PostgreSQL jobs;
- model-endpoint health checks;
- no implicit simultaneous loading of incompatible large models;
- resumable stages after endpoint restart.

## 18.2 Embedding optimization

The worker shall support microbatch embedding while preserving per-manifest lease ownership and completion.

UNVERIFIED performance target:

- demonstrate materially higher throughput than one HTTP request per chunk without increasing incorrect completion, lease loss, or dimension mismatch rates.

## 18.3 Memory constraints

Tests shall verify behavior under constrained system RAM and VRAM. The implementation must not assume that the entire candidate corpus or all retrieved passages fit in memory simultaneously.

## 19\. Non-Functional Requirements

## NFR-001 — Correctness

- No authoritative state may be inferred from Qdrant, Valkey, or scratch exports.
- Every state mutation must be transactionally valid.
- Semantic output must be schema-valid and referentially valid.
- Claims must not acquire stronger support labels than the evidence permits.

## NFR-002 — Auditability

A maintainer must be able to determine:

- what the system knew at each decision point;
- which model or rule made each proposal;
- why each action was permitted;
- which sources informed each claim;
- how the final report relates to exact source snapshots.

## NFR-003 — Resilience

- Lost Valkey notifications must not strand work.
- Qdrant may be rebuilt from PostgreSQL and blobs.
- Model endpoint failure must not corrupt run state.
- Search or extraction retries must be idempotent.
- A crash after external execution but before acknowledgment must be reconcilable.

## NFR-004 — Security

- credentials must not enter prompts, logs, exports, or model-visible metadata;
- scraped content must be treated as untrusted data;
- model outputs must not directly execute shell commands or SQL;
- URL canonicalization must remove or redact sensitive parameters;
- destructive operations require explicit scope and confirmation flags;
- report rendering must not execute source content.

## NFR-005 — Observability

Metrics shall include:

- runs by state and outcome;
- state-transition latency;
- search response counts;
- candidate counts;
- candidate triage coverage;
- extraction success by method;
- extraction-quality dispositions;
- coverage changes per iteration;
- search revisions per run;
- retrieval degradation;
- embedding queue age;
- lease loss and dead jobs;
- report validation failures;
- audit reuse and failure rates.

## NFR-006 — Maintainability

- orchestration logic shall not remain concentrated in a single thousand-line script;
- Bash shall be limited to thin launch adapters;
- embedded Python heredocs shall be removed from core workflows;
- domain logic shall have typed interfaces;
- migrations shall be reversible where safe;
- service modules shall be independently testable.

## 20\. Migration and Refactor Sequence

## Phase 1 — Semantic contract and state-machine foundation

### Scope

- add `ResearchSpec`, `SearchPlan`, `CoverageLedger`, `StrategyRevisionProposal`, and `EvidencePacket` models;
- add run-state machine;
- add execution mode;
- add deterministic budget policy;
- add semantic-call persistence;
- retain existing acquisition behavior behind adapters.

### Required code changes

- introduce typed domain modules;
- create initial database migrations;
- add transition service;
- add host-agent structured-input path;
- replace legacy automatic profile and complexity selection in the new path;
- retain legacy values only for comparison telemetry.

### Exit criteria

- a run can be created in either execution mode;
- a valid `ResearchSpec` can be proposed, validated, persisted, and versioned;
- invalid state transitions are rejected;
- the old workflow can be invoked through an adapter without losing current functionality.

## Phase 2 — Complete acquisition-corpus persistence

### Scope

- add search plans, queries, responses, candidates, and occurrences;
- persist every candidate before triage;
- store raw search response blobs;
- support replay of triage and extraction selection.

### Exit criteria

- deleting scratch data does not remove candidate history;
- a candidate can be traced to every query branch where it appeared;
- triage can be rerun without another live search;
- search-response ingestion is transactional and idempotent.

## Phase 3 — Coverage-led adaptive workflow

### Scope

- create coverage items from `ResearchSpec`;
- update coverage after acquisition and extraction;
- implement strategy-revision proposals;
- remove page-count-led stopping from the new path;
- require gap-targeted additional actions.

### Exit criteria

- every additional query or extraction wave references unresolved coverage items;
- a sufficient run can stop without reaching nominal page targets;
- an insufficient run cannot claim completion merely because enough pages succeeded;
- full iteration history is persisted.

## Phase 4 — PostgreSQL authority consolidation

### Scope

- migrate Catalog run state, events, annotations, assessments, and claim manifests into PostgreSQL;
- make filesystem Catalog output derived;
- make audit scheduling idempotent;
- consolidate run lifecycle operations.

### Exit criteria

- PostgreSQL alone reconstructs the complete run;
- Catalog exports regenerate from committed state;
- no split-brain lifecycle is possible;
- equivalent audit requests reuse current assessments.

## Phase 5 — Extraction and parsing refactor

### Scope

- introduce extraction-attempt records;
- implement deterministic quality metrics;
- replace regex-first HTML fallback;
- modularize parser and chunker interfaces;
- add real tokenization;
- preserve raw and normalized derivations.

### Exit criteria

- short but valid sources are not rejected solely for length;
- long blocked pages are not accepted solely for length;
- every normalized document references its extraction attempt;
- parser and chunker upgrades produce versioned derivations.

## Phase 6 — Retrieval and evidence completion

### Scope

- make degraded retrieval explicit;
- log all retrieval stages;
- remove unused Qdrant sparse configuration unless implemented;
- build corroboration, contradiction, qualification, and duplicate groups;
- add claim-evidence bindings;
- implement complete evidence packet.

### Exit criteria

- retrieval mode and degradation are observable;
- evidence packet fields are substantively implemented;
- each report claim can be traced to passages;
- duplicate sources do not inflate corroboration.

## Phase 7 — Synthesis, optimization, and legacy retirement

### Scope

- implement autonomous-local synthesis;
- batch embeddings;
- add semantic caching;
- benchmark local and host-agent modes;
- deprecate or remove dead legacy planner and classifier paths;
- simplify wrappers.

### Exit criteria

- autonomous-local mode produces validated reports;
- agent-led mode produces complete bounded evidence packets;
- embedding throughput and resource use are benchmarked;
- legacy behavior is either removed or explicitly compatibility-only.

## 21\. Test Strategy

## 21.1 Unit tests

Required coverage:

- schema validation;
- state transitions;
- budget policy;
- query validation;
- candidate identity;
- occurrence aggregation;
- extraction-quality metrics;
- coverage-state updates;
- strategy-revision authorization;
- evidence-reference validation;
- audit cache identity;
- export generation.

## 21.2 PostgreSQL integration tests

Use an explicitly disposable test database.

Test:

- migrations;
- transaction rollback;
- idempotent run creation;
- transition concurrency;
- duplicate search-response ingestion;
- candidate occurrence preservation;
- extraction savepoints;
- coverage revisions;
- report persistence;
- audit invalidation;
- compatibility export regeneration.

## 21.3 Qdrant integration tests

Test:

- index fingerprint creation;
- collection schema;
- alias activation;
- degraded-mode reporting;
- missing collection;
- point reconciliation;
- reindexing;
- rollback;
- candidate retrieval.

## 21.4 Valkey tests

Test:

- lost notification;
- expired notification;
- unavailable Valkey;
- finite wait;
- PostgreSQL polling recovery;
- cache pruning.

## 21.5 Semantic contract tests

Use recorded model outputs and deterministic fixtures.

Test:

- valid proposals;
- malformed JSON;
- missing fields;
- invented IDs;
- stale run revision;
- unauthorized scope expansion;
- unsupported execution-mode changes;
- contradictory coverage decisions;
- invalid claim-evidence bindings.

## 21.6 End-to-end scenarios

At minimum:

1.  Simple current fact lookup.
2.  Complex technical troubleshooting.
3.  Legislative or legal research.
4.  Breaking-news research.
5.  Academic debate with conflicting evidence.
6.  Retained-corpus-only answer.
7.  Search returns many irrelevant candidates.
8.  Search returns no candidates.
9.  Scrapes fail but metadata remains useful.
10. Qdrant unavailable.

11. Local LLM unavailable mid-run.

12. Run resumes after process crash.

13. Run stops partial after budget exhaustion.

14. Agent-led and autonomous-local runs use the same objective.

15. Scratch and Catalog exports are deleted and regenerated.

## 21.7 Fault-injection tests

Inject failures:

- after search response but before candidate commit;
- after candidate commit but before acknowledgment;
- after raw blob write but before transaction commit;
- after Qdrant upsert but before manifest completion;
- after report generation but before report registration;
- during audit stage persistence;
- during compatibility export.

## 21.8 Evaluation campaign

Create a fixed benchmark suite containing:

- research objectives;
- expected question decomposition;
- expected source classes;
- known relevant sources;
- known distractor sources;
- expected unresolved controversies;
- citation-support labels.

Compare:

- legacy workflow;
- new agent-led workflow;
- new autonomous-local workflow;
- deterministic-debug baseline.

Metrics:

- candidate recall;
- relevant-source precision;
- primary-source recall;
- coverage completeness;
- unsupported-claim rate;
- citation correctness;
- contradiction detection;
- acquisition cost;
- LLM call count;
- total latency;
- extraction success;
- report quality under blinded review.

All quality targets remain **UNVERIFIED** until benchmark baselines are collected.

## 22\. Acceptance Criteria for the Complete Refactor

The priority refactor is complete only when all of the following are true:

1.  PostgreSQL is the sole authoritative source for run and invocation state.
2.  A complete run can be reconstructed without scratch or Catalog files.
3.  `agent_led` and `autonomous_local` are explicit and tested.
4.  Every search candidate is persisted regardless of scrape selection.
5.  Every adaptive action references a persisted coverage gap.
6.  Page counts no longer independently determine completion.
7.  Search recovery requires a structured proposal and deterministic validation.
8.  Extraction attempts and quality metrics are persisted.
9.  Raw and normalized source data remain distinguishable.
10. Retrieval degradation is explicit.

11. Every retrieval stage is traceable.

12. Evidence packets contain implemented corroboration, contradiction, qualification, and duplicate analysis.

13. Reports bind claims to evidence IDs.

14. Semantic audits are persisted and idempotent.

15. Compatibility artifacts are regenerable exports.

16. Qdrant remains rebuildable.

17. Valkey loss cannot strand durable work.

18. Existing snapshot, derivation, index-manifest, and lease-safety guarantees do not regress.

19. The test and evaluation campaigns pass their defined release gates.

20. Legacy code paths are either removed or clearly marked compatibility-only.

## 23\. Rollout Strategy

## 23.1 Feature flags

Introduce flags for:

- authoritative workflow service;
- complete candidate persistence;
- coverage-led gating;
- new extraction pipeline;
- new evidence builder;
- PostgreSQL-backed audits;
- autonomous-local synthesis.

## 23.2 Shadow mode

Before changing default behavior:

1.  run the new planning and coverage components in shadow mode;
2.  preserve existing execution decisions;
3.  compare proposed actions with legacy actions;
4.  record divergences;
5.  evaluate on fixed benchmark objectives.

## 23.3 Dual-write period

A temporary dual-write period may write:

- authoritative PostgreSQL state;
- legacy compatibility artifacts.

PostgreSQL must be treated as authoritative from the point the feature flag is enabled. Dual-write disagreement must be surfaced.

## 23.4 Default transition

Recommended order:

1.  new state machine;
2.  full candidate persistence;
3.  coverage-led gating;
4.  PostgreSQL audit authority;
5.  extraction refactor;
6.  evidence and synthesis;
7.  legacy removal.

## 23.5 Rollback

Rollback may disable new orchestration behavior, but must not discard new authoritative records.

Schema rollback shall not be used when it would destroy valid research history. Prefer forward repair migrations.

## 24\. Risks and Mitigations

# Product Requirements Document

| Risk                                          | Impact | Mitigation                                                        |
| --------------------------------------------- | ------ | ----------------------------------------------------------------- |
| Overlarge refactor creates regressions        | High   | Phase gates, adapters, shadow mode, fixed benchmarks              |
| PostgreSQL schema becomes overly coupled      | High   | Typed service boundaries and append-only event records            |
| Local LLM produces unstable structured output | High   | Strict schemas, retries, bounded stages, deterministic validation |
| Coverage model becomes too subjective         | High   | Separate objective metrics from semantic assessments              |
| Candidate corpus grows rapidly                | Medium | Retention policy, compressed raw blobs, indexed metadata          |
| Too many LLM calls increase latency           | Medium | Learned prefilters, semantic caching, bounded calls               |
| Dual-write state diverges                     | High   | PostgreSQL authority and disagreement alerts                      |
| Parser changes alter retrieval behavior       | Medium | Versioned derivations and benchmark comparison                    |
| Search-provider metadata changes              | Medium | Raw-response retention and versioned adapters                     |
| Agent-led and local modes drift               | High   | Shared schemas and identical deterministic services               |
| Audit model validates its own mistakes        | Medium | Deterministic reference checks and optional independent evaluator |
| Coverage gate loops indefinitely              | High   | Hard budgets, maximum revisions, loop detection                   |
| Source duplicates inflate confidence          | High   | Candidate and content similarity grouping                         |
| Retrieval silently degrades                   | High   | Required degradation metadata and health state                    |

## 25\. Coding AI Agent Implementation Rules

The coding agent shall follow these constraints.

## 25.1 Repository discipline

1.  Inspect the current implementation before modifying a subsystem.
2.  Preserve existing invariants unless this PRD explicitly changes them.
3.  Prefer small, reviewable commits grouped by one requirement.
4.  Do not combine schema migration, orchestration rewrite, and unrelated cleanup in one commit.
5.  Do not delete legacy behavior until the replacement has passing integration tests.
6.  Do not add dependencies without documenting purpose and operational impact.
7.  Do not place new core logic in Bash or embedded Python heredocs.
8.  Do not hard-code the user’s local endpoint addresses in domain logic.

## 25.2 Source-of-truth discipline

Before each implementation phase, identify:

- applicable GSP decisions;
- functional requirements being implemented;
- existing invariants being preserved;
- migration consequences;
- tests required before completion.

## 25.3 Change format

For each implementation task, the coding agent shall provide:

- affected requirements;
- files changed;
- before and after behavior;
- schema changes;
- migration behavior;
- backward-compatibility impact;
- tests added;
- known limitations;
- rollback or repair procedure.

## 25.4 Safety rules

The coding agent shall not:

- infer successful persistence from file creation;
- mark semantic proposals accepted without policy validation;
- silently catch authoritative-state errors;
- silently suppress retrieval degradation;
- invent source or evidence IDs;
- weaken lease, snapshot, or content-hash guarantees;
- permit a model response to execute arbitrary code;
- make destructive migration behavior the default.

## 25.5 Completion rule

A task is not complete because code compiles.

A task is complete only when:

- the relevant requirements are implemented;
- unit tests pass;
- integration tests pass where applicable;
- failure paths are tested;
- migrations are documented;
- observability is present;
- compatibility effects are recorded.

## 26\. Recommended Initial Issue Hierarchy

## Epic 1 — Authoritative research state

- Define state-machine enums and transitions.
- Add research-run transition table.
- Add invocation and event tables.
- Implement `ResearchRunService`.
- Add idempotency and concurrency tests.
- Route `frun` through the service.

## Epic 2 — Semantic contracts

- Define `ResearchSpec`.
- Define `SearchPlan`.
- Define `CandidateAssessment`.
- Define `CoverageLedger`.
- Define `StrategyRevisionProposal`.
- Define `EvidencePacket`.
- Extend model gateway provenance.

## Epic 3 — Complete acquisition persistence

- Add query and response tables.
- Add candidate and occurrence tables.
- Persist raw search responses.
- Add candidate replay APIs.
- Update `fsearch` adapter.

## Epic 4 — Coverage-led orchestration

- Generate initial coverage items.
- Apply candidate and evidence events.
- Implement coverage projection.
- Implement next-action proposal validation.
- Replace successful-page stopping.

## Epic 5 — Catalog consolidation

- Add database-backed annotations.
- Add claim manifest tables.
- Add database-backed staged audits.
- Add compatibility export.
- Remove duplicate audit scheduling.

## Epic 6 — Extraction modernization

- Add extraction-attempt table.
- Add quality metrics.
- Add DOM-aware HTML extraction.
- Add parser interfaces.
- Add tokenizer-backed chunks.
- Preserve derivation lineage.

## Epic 7 — Retrieval and evidence

- Add retrieval degradation contract.
- Log all retrieval stages.
- remove or implement Qdrant sparse vectors;
- implement evidence grouping;
- implement claim-evidence binding.

## Epic 8 — Synthesis and optimization

- Implement autonomous-local synthesis.
- Implement report validation.
- Implement embedding batches.
- Add semantic cache.
- Run benchmark campaign.
- retire obsolete legacy paths.

## 27\. Definition of Done

The project is done when the implementation satisfies the complete acceptance criteria in Section 22 and the benchmark campaign demonstrates that the new architecture:

- does not regress deterministic corpus integrity;
- improves workflow traceability;
- preserves more of the acquired candidate corpus;
- makes adaptive decisions explainable;
- reduces unsupported report claims;
- operates successfully in both approved execution modes.

No benchmark result should be represented as established until measured. Any unmeasured quality or performance claim must remain labeled **UNVERIFIED**.

## 28\. Deliverable Self-Check

- **No fabrication:** Repository behavior cited in the current-state sections is grounded in inspected source.
- **Approved decisions preserved:** GSP-A1, GSP-A2, and GSP-A3 govern all requirements.
- **Claim strength:** Performance and quality improvements are specified as targets or evaluation requirements, not established results.
- **Scope control:** The PRD preserves the existing deterministic corpus and index guarantees and focuses refactoring on workflow authority, acquisition persistence, coverage, extraction, evidence, and synthesis.
- **Traceability:** Functional requirements, acceptance criteria, migration phases, service boundaries, and coding-agent rules are explicitly mapped.
- **Limitations:** Runtime behavior and performance remain UNVERIFIED pending the prescribed tests and benchmark campaign.
