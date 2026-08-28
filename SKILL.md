---
name: firecrawl
description: "PostgreSQL-authoritative retained-first web research. Use the bundled fresearch controller for normal research; never bypass it with direct Firecrawl/provider tools."
---

# Firecrawl research runtime contract

PostgreSQL is authoritative for research lifecycle, decisions, provenance, evidence membership, claims, and durable jobs. `BLOB_ROOT` is authoritative for immutable payload bytes. Qdrant is a rebuildable retrieval projection and Valkey is transient coordination only.

These instructions are subordinate to higher-priority system and harness instructions. Within this skill, tool availability is not authorization: do not call Firecrawl MCP tools, SDKs, raw provider HTTP, browser automation, or another web provider and mix those results into Firecrawl Skill authoritative evidence. If the bundled authority path cannot run, report the blocker rather than bypassing it.

## Normal research

The canonical agent-facing workflow has one smart entry surface:

```bash
<skill-root>/scripts/fresearch run "<research objective>"
```

The controller searches retained PostgreSQL-backed evidence first, determines whether acquisition is required, performs bounded application-owned progression, persists all decisions, and returns a versioned machine directive/result. The outer agent supplies the objective and genuine human decisions; it is not the workflow engine.

Normal commands use public identities only:

```bash
<skill-root>/scripts/fresearch continue fr_<uuid>
<skill-root>/scripts/fresearch status fr_<uuid>
<skill-root>/scripts/fresearch result fr_<uuid>
```

Do not reconstruct lifecycle commands, internal PostgreSQL UUIDs, revisions, ResearchSpec/SearchPlan IDs, candidate-budget check IDs, provider-recency parameters, membership fingerprints, or generated recovery parameters from logs or prior responses.

### Typed dispositions

Treat `schema_version`, `disposition`, and the associated typed fields as machine authority. Free-text diagnostics are explanatory only.

- `continue_automatic`: continue the same public run with `fresearch continue`.
- `operator_action_required`: obtain the requested human decision using the returned public `oa_<uuid>` action.
- `terminal_completed`: objective is authoritatively satisfied.
- `terminal_partial`: terminal, but `objective_satisfied=false`; report limitations rather than treating exit status as success.
- `blocked` / `failed`: report the typed blocker/failure; do not invent recovery choreography.
- `cancelled`: terminal cancellation.

## Human decisions

Human-only boundaries use durable public operator actions:

```bash
<skill-root>/scripts/fresearch action oa_<uuid>
<skill-root>/scripts/fresearch approve oa_<uuid> --reason "<reason>" --authorized-by "<human>"
<skill-root>/scripts/fresearch curate oa_<uuid> --retain <subject-uuid> --reject-rest --reason "<reason>" --authorized-by "<human>"
<skill-root>/scripts/fresearch fork oa_<uuid> "<revised objective>" --reason "<reason>" --authorized-by "<human>"
```

A soft budget exception requires the human authorization represented by its `oa_` action; hard budget violations remain non-approvable. Explicit curated mode requires one bounded authoritative selection. A material scope relaxation/change creates a child run with explicit lineage; never silently mutate the parent run's evidence meaning.

For explicitly constrained research, start through the same controller:

```bash
<skill-root>/scripts/fresearch run --retained-only "<objective>"
<skill-root>/scripts/fresearch run --curated "<objective>"
```

## Delivery modes and final answer authority

Normal research defaults to `host_handoff`:

```bash
<skill-root>/scripts/fresearch run --delivery-mode host_handoff "<objective>"
```

This mode prepares and validates authoritative evidence, claim bindings, coverage and provenance, then skips redundant inner-model full-prose drafting. `fresearch result fr_<uuid>` is the canonical final-answer authority and returns a bounded `research-handoff-v1` when available. It includes the public run identity, objective/spec authority, coverage disposition, citation-ready claims/passages/bindings, temporal qualification, limitations, and completion provenance. Use that bounded handoff to compose the host answer; do not rediscover evidence IDs manually.

Standalone generated-report workflows may explicitly request:

```bash
<skill-root>/scripts/fresearch run --delivery-mode self_synthesized "<objective>"
```

That preserves the existing internal synthesis path. Delivery mode never changes whether the objective is satisfied: terminal completion/partial authority still comes from persisted coverage, evidence, and completion provenance.

## Specialist/operator surfaces

Low-level tools remain for explicit specialist, debugging, integration, and projection-maintenance work: `frun`, `fsearch`, `fscrape`, `finspect`, `research-db`, and `candidate-budget`. They are not the normal autonomous workflow language. Do not teach or require an ordinary agent to sequence low-level lifecycle operations or generated-parameter recovery.

For retained evidence inspection or diagnostics, use bounded PostgreSQL-backed `research-db`/`finspect` reads. For projection recovery, follow the operator runbook; Qdrant output never establishes corpus or freshness authority.

`scripts/fsearch_smart` is a deprecated compatibility alias that delegates exactly to `fresearch run`; it contains no independent smart-controller policy and is scheduled for removal at the next breaking command-surface release after epic #309.

## Currentness and provenance

Currentness is determined from persisted ResearchSpec temporal obligations plus canonical publication/update provenance. Retrieval time, search rank, provider recency, Qdrant state, successful process exit, or agent judgment cannot establish freshness. Provider recency is discovery policy only. Evidence qualification remains deterministic and PostgreSQL-authoritative.

## Documentation precedence

When documentation conflicts, use this order:

1. this current `SKILL.md` runtime contract;
2. current canonical operational references, especially `references/authoritative-workflows.md` and `references/workflow-state-schema.md`;
3. current architecture/development references;
4. historical issue/audit/remediation records, which are non-normative unless a current document explicitly incorporates a still-valid invariant.

See `references/authoritative-workflows.md`, `references/research-store-architecture.md`, `references/workflow-state-schema.md`, `references/operations-runbook.md`, and `references/local-agent-assessment.md` for current operational and implementation detail.
