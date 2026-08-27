# Issue #307 smart-search temporal audit remediation

> **HISTORICAL / NON-NORMATIVE.** This file preserves issue #307 design/review history. Current runtime and operator semantics are defined by `SKILL.md`, `references/authoritative-workflows.md`, and `references/workflow-state-schema.md`. Temporal evidence invariants remain applicable only where those current authorities and current source incorporate them; the old `fsearch_smart` exit-75/recovery command language below has been superseded by controller-owned progression and durable public `oa_<uuid>` actions.

This reference records the post-audit authority contract for issue #307. It is a
behavioral boundary and validation map, not a historical backlog. PostgreSQL
remains workflow authority; Qdrant remains a rebuildable projection; provider
recency remains discovery-only.

## End-to-end authority flow

```text
raw objective
  -> smart-objective-intent-v1 structured local semantic interpretation
  -> strict JSON Schema + deterministic cross-field validation
  -> deterministic ResearchSpec materialization
  + independent SearchPlan discovery window
  -> exact-local provider-recency projection
  -> bounded acquisition and deterministic candidate temporal admission
  -> canonical temporal provenance extraction/reconciliation
  -> evidence preparation / temporal qualification
     -> qualifying evidence: normal lifecycle
     -> TemporalCoverageUnsatisfied: durable recoverable gap
        -> reacquire while budget remains
        -> operator action at exhaustion
```

The local model interprets semantic meaning only. It does not own UUIDs, current
time, relative-date arithmetic, provider `tbs`/`qdr` parameters, temporal evidence
qualification, candidate admission, budget policy, or scope exceptions.

## Blocking findings resolved

### Semantic objective contract

`smart-objective-intent-v1` is a strict, versioned contract with
`additionalProperties: false`. In addition to the exact original objective and
temporal semantics, it carries bounded research questions, entities,
jurisdictions, and user constraints. Deterministic materialization:

- validates nonblank/unique semantic lists;
- creates stable question IDs outside the model;
- preserves entities, jurisdictions, and user constraints in `ResearchSpec`;
- resolves relative temporal arithmetic against one explicit evaluation clock;
- separates evidence obligations from the discovery window; and
- supplies the materialized `ResearchSpec` scope to the downstream query planner.

Normal `autonomous_local` interpretation failure is fail-closed and points to an
explicit `--research-spec`; regex parsing is not an autonomous recovery path.
`deterministic_debug` retains the deliberately narrow fallback grammar. That
degraded grammar intentionally supports compact bounded forms such as
`August 18-23, 2026`; natural-language forms such as
`from August 18 to August 23, 2026` remain semantic-primary rather than being
added to an open-ended regex language.

### Typed temporal evidence boundary

Zero qualifying temporal passages are represented by
`TemporalCoverageUnsatisfied`, which owns bounded `TemporalCoverageDiagnostics`.
The class is intentionally distinct from generic `EvidencePreparationError`.
Smart resume catches that exact type, persists the stable gap payload, and
continues bounded repair or returns operator action at budget exhaustion.

A generic evidence error is never re-diagnosed from passage timestamps. This is
a safety invariant: an unrelated semantic extraction, packet validation, or
coverage-state failure cannot be hidden behind temporal recovery merely because
the same corpus also lacks qualifying timestamps.

### Explicit temporal provenance

`temporal_candidate` and `temporal_corpus` keep publication, update,
provider-generic date, HTTP metadata, and retrieval time separate. The bounded
extractor recognizes explicit JSON-LD, HTML metadata/`time` markers, explicit
page-visible `Published`/`Updated` forms, and HTTP `Last-Modified`. Nested
structured records are traversed under hard limits so live-blog/post entries can
retain per-entry temporal provenance.

Candidate/request/document observations are reconciled fail-closed. Invalid or
genuinely conflicting explicit publication **or update** signals remain unknown;
no source wins by precedence. Equality is temporal, not textual: timestamps that
represent the same aware instant with different UTC offsets corroborate one
canonical UTC instant while their raw values and offsets remain in provenance.
Distinct instants still conflict. Retrieval time and generic provider `date`
never become publication authority.

## Important non-blocking findings resolved

- The former duplicate unbounded-discovery helpers are consolidated in
  `smart_objective_intent.unbounded_discovery_window`; search-plan materialization
  imports that canonical helper rather than duplicating policy text.
- Update provenance now follows the same fail-closed cross-source conflict rule
  as publication provenance instead of selecting the candidate value by
  precedence.
- The generic operator-action fallback is explicit and tested:
  `inspect_operator_action_then_resume_same_run`. Known action kinds retain their
  narrower recovery strings.
- Temporal operator output omits zero-valued reason noise where practical and
  explicitly reports that automatic scope relaxation is false plus the persisted
  required-resolution contract.

## Independent review follow-up remediation

Independent review of PR #308 identified four additional invariants that are now
part of this contract:

- **Response-scoped admission is immutable.** The first persisted
  `acquisition.temporal_admission` event is authoritative for that exact
  `search_response_id`. Replaying an idempotent response reuses that assessment
  snapshot and its persisted `responded_at`; it does not re-evaluate the older
  response against a canonical candidate row that may have been updated by a
  later response.
- **Equivalent offsets are not conflicts.** Explicit timestamps are compared as
  timezone-aware UTC instants. Raw signal strings remain provenance; only
  genuinely different instants create a conflict.
- **Freshness diagnostics match qualification semantics.** Multiple
  `max_age_days` requirements are conjunctive. Staleness classification therefore
  uses the strictest (smallest) allowed age, while discovery planning may still
  use the broader non-narrowing window needed to cover all requested evidence.
- **Canonical resume has no facade back-edge.** `orchestration.resume` is typed
  against the orchestration-owned `ResumeOrchestratorPort`; it has no runtime or
  type-only import of `smart_orchestrator`.

These changes are corrections to replay/provenance/diagnostic/dependency
semantics. They do not add a temporal waiver, alter provider authority, weaken
publication-window rules, or move authority away from PostgreSQL.

## Required semantic distinctions

| Objective intent | Evidence obligation | Discovery |
|---|---|---|
| `latest ... past 5 days` | freshness `max_age_days=5`; publication **or authoritative update** may qualify | rolling five days |
| `articles published between ...` | strict publication window; qualifying `published_at` required | explicit interval |
| explicit conjunction | publication window **and** freshness both required | non-narrowing superset of both |
| no temporal intent | no temporal evidence obligation | unbounded |

Provider success within a coarse recency filter is never evidence qualification.
For example, exact-local `qdr:5d` may project to Firecrawl `qdr:w`, but a result
from that provider window still must satisfy the persisted local ResearchSpec.

## Candidate and triage contract

Candidate temporal admission is bound to one persisted search response. First
execution evaluates canonical temporal authority against that response's exact
persisted `responded_at` and persists the resulting
`acquisition.temporal_admission` event. Replays reuse that response-scoped event
rather than mutable later candidate state. Known explicit out-of-scope candidates
are removed before scrape scheduling. Unknown candidates may be investigated
under the normal bound but cannot satisfy temporal coverage until explicit
authority is established. Semantic triage receives bounded
`temporal_assessment` data and cannot override `ineligible`.

## Recoverable coverage contract

A temporal gap payload contains:

- `kind=temporal_coverage_gap`;
- `status=unsatisfied`;
- `recoverable=true`;
- coverage revision;
- bounded reason census;
- `automatic_scope_relaxation=false`;
- `scope_relaxation_requires=persisted_research_spec_revision`; and
- an explicit required resolution.

While adaptive acquisition budget remains, the same run may acquire additional
qualifying evidence. At exhaustion the CLI exits 75 with
`resolve_temporal_coverage_gap_then_resume_same_run`; it does not mark the run a
successful completion and does not convert nonqualifying evidence into evidence.
A user-approved scope change is a new persisted ResearchSpec revision.

## Regression map

The issue-307 regression set must demonstrate at minimum:

1. the audited `latest ... within the past 5 days` form is freshness semantics
   without a false publication window;
2. the exact audited raw objective enters the production
   `resolve_objective_spec` -> `interpret_smart_objective` structured path, with
   only the local semantic transport stubbed in deterministic tests;
3. semantic questions/entities/jurisdictions/user constraints survive into the
   ResearchSpec and planner context;
4. strict schema/post-validation rejects changed objective, missing/duplicate
   semantic scope, ambiguity, contradiction, and invalid temporal combinations;
5. autonomous semantic failure occurs before provider acquisition and never
   invokes regex fallback;
6. deterministic-debug fallback remains narrow and accepts only sanctioned
   redundant wording;
7. freshness-only, publication-only, conjunctive, and unbounded discovery cases
   remain distinct, including exact-local/provider-superset recency behavior;
8. response A -> later response B mutating the same canonical candidate -> replay
   A preserves A's response ID, admission membership, assessment, and persisted
   event payload;
9. old-publication/recent-update behavior differs correctly under freshness and
   publication-window requirements;
10. generic provider dates and retrieval timestamps never become publication;
11. equivalent timestamp offsets corroborate one canonical instant while truly
    different explicit instants fail closed;
12. document metadata covers published-only, updated-only, both, missing,
    malformed/conflicting, page-visible update, and live-blog nested fixtures;
13. cross-source publication and update conflicts fail closed;
14. multiple freshness requirements classify stale evidence against the strictest
    conjunctive requirement;
15. typed temporal insufficiency is raised at the evidence boundary with bounded
    diagnostics;
16. smart resume recovers **only** the typed temporal exception and never
    reclassifies a generic evidence-preparation error;
17. reacquisition/operator-action and persisted gap/resolution semantics survive
    restart/replay;
18. LLM triage cannot override deterministic candidate ineligibility;
19. the canonical resume module has no runtime or type-only dependency on
    `smart_orchestrator`; and
20. CLI and `finspect` surfaces remain bounded and expose attempt/failure and
    temporal-gap disposition without requiring unbounded history dumps.

## Validation gate

The exact candidate head is acceptable only after repository-authoritative local
validation completes in this order:

1. Ruff check and format-check on every changed Python file against the issue
   base;
2. changed-scope Pyrefly including changed tests;
3. focused issue-307 plus affected #300/#301/#302/#305 tests;
4. full-project `pyrefly check` with no baseline drift;
5. service-backed PostgreSQL/Qdrant validation using
   `references/local-disposable-test-services.md` and
   `scripts/disposable-test-services` under a unique namespace;
6. the response-mutation/replay PostgreSQL regression under that disposable
   service contract;
7. relevant broader unit/integration suites and the authoritative
   fsearch/storage/fscrape/orchestration boundary families;
8. `git diff --check` and complete base-to-head diff audit;
9. disposable-service teardown and no-owned-container verification; and
10. exact-head GitHub CI plus final head readback.

No local validation result from an earlier head may be attributed to a later
source revision.
