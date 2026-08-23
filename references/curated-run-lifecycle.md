# Curated direct-acquisition lifecycle

Curated runs provide an explicit operator-controlled path for direct Firecrawl
research while preserving PostgreSQL as the authority for lifecycle,
invocation provenance, extraction budgets, asset promotion, exact completion
membership, evidence, synthesis, and terminal completion. `BLOB_ROOT` remains
the immutable payload store. Qdrant remains a rebuildable projection and Valkey
is optional transient coordination.

Issue #300 extends the original curated-run contract with a supported post-seal
evidence/synthesis phase and exact temporal provenance rules. It does not add a
second lifecycle or completion authority.

## Run modes and execution authority

`frun start` accepts `--run-mode autonomous|curated`. The independent `--mode`
option selects semantic execution authority:

- `autonomous_local`: semantic work must use the configured local generative
  endpoint/model;
- `agent_led`: semantic work requires an explicit host artifact supplier;
- `deterministic_debug`: test fixtures only and not accepted as production
  curated synthesis authority.

The declared run mode is stored in `research_runs.metadata.run_mode`. Historical
rows without it are reported as `legacy_unspecified`; no migration infers or
backfills historical mode, lifecycle state, or provenance.

## Canonical curated sequence

A direct wrapper invocation is legal only while the run is in `acquiring`.
Neither `fsearch` nor `fscrape` implicitly advances the run lifecycle.

```text
frun start "objective" --run-mode curated --mode autonomous_local
frun prepare <fr_id>
fsearch ... --research-run-id <fr_id>
fscrape ... --research-run-id <fr_id>
frun assets <fr_id>
frun retain <fr_id> <promotion_subject_id>
frun reject <fr_id> <promotion_subject_id> --reason "..."
frun seal-acquisition <fr_id>
frun resume <fr_id>                         # complete index checkpoint
frun synthesize <fr_id>                     # evidence + five synthesis stages
frun finish <fr_id> --outcome satisfied     # separate terminal admission
```

`frun synthesize` is mandatory before a satisfied curated completion unless the
same authoritative EvidencePacket and synthesis stages already exist and pass
the reuse checks below. `frun synthesize` never calls `frun finish`, never
changes a successful result to another terminal outcome, and never performs
search expansion.

## Preparation and direct invocation

`frun prepare` is the explicit, idempotent transition from `created`,
`planning`, or `corpus_review` to `acquiring`. Planning initializes its
ResearchSpec/search-plan tuple before acquisition/provider preflight is applied.
Provider preflight remains mandatory at the actual provider boundary.

The production `fsearch` builder and production `fscrape` path revalidate the
run and lifecycle revision immediately before authoritative invocation
persistence. A lifecycle transition cannot interleave between the locked
authority check and the invocation start record. If the run is no longer
acquisition-eligible, no provider call is permitted and no invocation or start
event is committed.

Every direct invocation persists the locked lifecycle revision in
`research_invocations.lifecycle_revision`, repeats lifecycle state/revision in
invocation JSONB metadata, and records the append-only `invocation_started`
event. Direct scrape additionally records the `direct_scrape_started` event at
its authoritative application boundary. These records are provenance, not a
second lifecycle authority.

## Direct scrape idempotency and hard extraction budget

`fscrape` derives its default logical identity from the run and normalized,
content-affecting scrape request. Generated invocation identity is not part of
the logical request key.

Consequently:

- the same logical request in the same run replays a terminal prior success or
  failure without another provider call or extraction-attempt row;
- an explicit caller idempotency key remains authoritative and conflicting
  request semantics under that key fail closed;
- requests never deduplicate across runs;
- `fscrape --fresh` deliberately creates a new immutable invocation/provenance
  lineage when new work is required.

The stable `fscrape` result reports `fresh_requested`, `fresh_effective`,
`fresh_parent_invocation_id`, and `work_mode` (`new`, `fresh`, or `replay`).
`fresh_requested` records operator intent; `fresh_effective` is true only when
`--fresh` selected a fresh idempotency identity rather than an explicit
caller-owned key. Effective fresh work links to the newest prior terminal
invocation with the same normalized logical input, excluding the selected fresh
key itself so an idempotent replay cannot become its own parent. This makes a
fresh retry after failure and a refresh after success auditable without
conflating generated invocation identity with logical request identity.

Fresh direct scrape work is admitted under a run-scoped PostgreSQL serialization
lock. The service reads the authoritative extraction-attempt count and projected
fresh work before provider access. A projection above the hard extraction limit
is rejected with current count, hard limit, projected count, and remaining
headroom. Replays cost zero. The hard limit cannot be overridden and a rejected
admission creates neither a provider call nor a fabricated failed extraction
attempt.

## Asset curation, completion admission, and membership seal

`frun assets` is the authoritative discovery surface for stable promotion
subject UUIDs. Operators use those subject IDs with `retain` and `reject`;
snapshot IDs, ranks, URLs, and local filenames are not substitutes. A promotion
subject belonging to another run is rejected even if its snapshot or URL looks
compatible. The curated asset surface never discovers candidates; it only
projects already persisted PostgreSQL acquisition authority.

Before `seal-acquisition` advances `acquiring -> extracting -> indexing`, it
persists and revalidates the completion-admission preview against the exact
retained set while holding the authoritative run lock. Hard-limit violations
are never overrideable. Any soft override is bound to the exact persisted
preview and predecessor revision.

`seal-acquisition` then promotes admitted assets through evidence eligibility,
seals exact PostgreSQL completion membership, and binds the chunk set used by
indexing. Re-running a completed seal is idempotent. A changed
completion-critical set, stale lifecycle revision, missing compatible chunks,
or historical unstructured asset fails closed rather than being inferred.

If a seal operation is interrupted after the lifecycle has reached `extracting`
or `indexing` but before an active membership seal exists, `frun resume` does
not start checkpoint processing. `frun finish` fails closed, and rerunning
`frun seal-acquisition` completes only the missing seal work without repeating
the `extracting` or `indexing` transitions. Only after an active seal exists may
checkpoint processing resume.

## Index resume and the synthesis boundary

For a curated run, `frun resume` is mode- and seal-aware:

- `created`, `planning`, or `corpus_review`: next action is `frun prepare`;
- `acquiring` or `extracting`: next action is `frun seal-acquisition`;
- `indexing` without an active seal: next action is `frun seal-acquisition`;
- `indexing` with an active seal: resume the bounded index checkpoint;
- `coverage_review` or `synthesizing`: next action is `frun synthesize`;
- `validating`: `frun finish <fr_id> --outcome satisfied` is shown only when
  current completion provenance and temporal authority already pass read-side
  validation; otherwise the next action remains `frun synthesize`;
- terminal states: no further lifecycle action.

The index checkpoint remains independent of synthesis. `frun synthesize` is
accepted only after indexing has reached the supported post-index states and an
active non-empty completion membership seal exists. The command owns the legal
lifecycle progression: it enters `synthesizing` before evidence/semantic work,
keeps a failed or interrupted run in `synthesizing` for explicit retry, and
advances to `validating` only after all five synthesis stages (including the
deterministic validation stage) complete. A satisfied `frun finish` is rejected
until the curated run is in `validating`; there is no `coverage_review ->
completed` shortcut.

## Curated EvidencePacket preparation

`frun synthesize` composes the existing `EvidencePreparationService` rather
than duplicating evidence logic in the CLI. Its inputs are derived solely from
current PostgreSQL authority:

1. current persisted ResearchSpec;
2. current coverage projection;
3. active exact membership seal;
4. each sealed promotion subject's persisted candidate ID and exact sealed
   chunk IDs.

No URL, filename, rank, or heuristic matching is used to reconstruct identity.
The service first performs semantic-authority readiness checks, before evidence
preparation can invoke semantic work.

For `autonomous_local`, `GENERATIVE_URL` and `GENERATIVE_MODEL` must be
configured and a live `/models` probe must succeed. When the endpoint advertises
model identities, the configured model must be present. There is no silent
commercial fallback. `agent_led` requires a real host artifact supplier and
uses that host authority directly; it does not acquire or health-gate the local
generative resource merely to persist host-authored stages.

The current EvidencePacket may be reused only when all of the following match:

- current ResearchSpec identity;
- every packet passage is within the active sealed chunk membership;
- an append-only `curated_evidence_prepared` marker identifies the exact packet
  revision, membership SHA-256, and current curated-synthesis policy version.

Historical packets that cannot prove those relationships are rebuilt rather
than trusted by compatibility inference.

For an unbounded ResearchSpec, an EvidencePacket is not incomplete merely
because its sources expose no publication/update dates. Missing temporal dates
remain diagnostic background information. As soon as the ResearchSpec contains
a publication window or numeric freshness obligation, the ResearchSpec-aware
evidence and terminal guards require qualifying publication/update provenance
and fail closed if it is absent.

## Five-stage synthesis and resumability

After evidence preparation, the existing `LocalSynthesisService` runs its five
canonical stages:

1. `outline`;
2. `binding`;
3. `draft`;
4. `citation_pass`;
5. `validation`.

Completed stages for the current EvidencePacket revision are reusable. The
curated terminal-grade path deliberately bypasses the cross-run semantic result
cache when executing incomplete stages: cached content cannot substitute for
the current run-local semantic call/artifact identities required by completion
provenance. Failed stages remain resumable through the existing stage service.
If the authoritative EvidencePacket revision changes, stage rows pointing to
the old revision are reset to pending for the new packet before synthesis.
Their old semantic calls and immutable artifacts remain historical provenance;
pointers are not silently reused across evidence revisions.

A successful `frun synthesize` reports `frun finish <fr_id> --outcome satisfied`
as the next action. A failed/incomplete synthesis reports `frun synthesize
<fr_id>` for explicit retry. It does not terminalize the run.

## Exact recency and temporal provenance

One canonical recency parser owns request, provider-normalization, ranking, and
persistence semantics. Unsupported explicit syntax fails closed; it never
silently becomes a default 365-day window.

`qdr:5d` means exactly five days to local policy. Sub-day syntax is equally
exact: for example, `qdr:5h` is a five-hour local ranking window, not a rounded
one-day freshness allowance. Firecrawl itself is given the smallest documented
discovery filter that cannot exclude valid results (`qdr:w` for five days,
`qdr:d` for five hours). The broader provider result set is only discovery;
local ranking, evidence, and terminal policy enforce the exact requested
duration.

Temporal provenance is separated into three authorities:

- **publication** — explicit provider publication metadata, persisted to
  `search_candidates.published_at` and then `documents.published_at`;
- **update/modification** — explicit provider update metadata, persisted
  separately and propagated to snapshot `last_modified`;
- **retrieval** — the time this system fetched the asset.

A generic provider `date` field is retained as ambiguous `provider_date`; it is
not promoted to publication. Retrieval time is never inferred to be
publication.

An explicit ResearchSpec publication window can be satisfied only by an
explicit publication timestamp inside the window. A date-only end bound
includes the entire named day. A numeric `max_age_days` freshness requirement
may be satisfied by an explicit publication or explicit update timestamp within
the bound, but not by retrieval alone. Thus an old publication with a recent
update can satisfy a max-age freshness obligation while still failing a recent
publication-window obligation.

## Background evidence versus temporal satisfaction

When a ResearchSpec contains bounded temporal obligations, EvidencePreparation
partitions the sealed corpus into temporally qualifying and background
passages. Background/out-of-window/undated material may remain in the
EvidencePacket for context, but semantic claim assignment and authoritative
freshness satisfaction use only qualifying passages. If no passage qualifies,
evidence preparation fails before semantic claim extraction.

For each numeric freshness requirement, PostgreSQL coverage records both the
freshness observation and the exact passage IDs that satisfy that specific
coverage item. A fresh passage associated with one requirement does not
globally satisfy another requirement.

## Terminal completion guard

`frun finish --outcome satisfied` remains the independent checkpoint-guarded
terminal boundary. Inside the same UoW/run lock used for terminal commit it
revalidates existing completion provenance and then temporal obligations.

The temporal guard:

- is additive: a historical/mechanical run with no bound ResearchSpec has no
  temporal obligation;
- requires an EvidencePacket when the current ResearchSpec is temporally
  bounded and requires that packet's PostgreSQL `research_spec_id` to equal
  the run's current bound ResearchSpec row;
- requires every publication-window obligation to have qualifying published
  claim-bound evidence;
- for each `max_age_days` requirement, loads that requirement's exact
  `freshness_requirement` coverage item, requires both coverage and freshness
  status to be satisfied, requires non-empty exact passage IDs, verifies those
  passages are current claim-bound packet evidence, and rechecks their
  publication/update timestamps under the current bound.

Failure aborts the terminal transaction; no satisfied terminal decision is
committed. EvidencePacket revision writes acquire the same research-run row lock
before insertion, so a concurrent packet revision cannot pass the terminal
provenance/temporal reads while completion is committing.

## Schema and compatibility impact

No DDL migration is required for issue #300. The revisions use existing
PostgreSQL columns, JSONB provenance, coverage events, EvidencePacket revisions,
synthesis stage rows, and membership seals. The canonical composition root
continues to return `PostgresUnitOfWork`; issue-300 temporal strengthening lives
inside its shared connection-bound repository context instead of introducing a
second production UoW type.

The canonical coverage service also repairs a pre-existing compatibility bug in
which one batch idempotency key could collapse multi-item ResearchSpec seeding.
It reuses any already-created `(item_type, subject_id)` item and creates each
missing item with its own deterministic idempotency key. Historical rows are not
rewritten.

## Required validation

Before merge, the issue-specific gate must exercise all of the following:

- smart-run planning reaches acquisition eligibility without provider access
  during planning and preparation is idempotent;
- same logical `fscrape` request replays success/failure, explicit `--fresh`
  creates new lineage, and conflicting explicit keys fail closed;
- direct-scrape hard budget boundaries, replay-at-limit, and concurrent fresh
  admission cannot oversubscribe the run;
- exact `qdr:5d` local semantics with provider `qdr:w` discovery superset;
- explicit publication/update/generic-date/retrieval provenance remains
  distinct through search and direct-scrape ingestion;
- missing publication, retrieval-only, out-of-window, old-but-updated, and
  exact five-day boundary cases;
- multi-item coverage seeding creates and reuses every logical item;
- endpoint unavailable fails before curated semantic work;
- no-evidence curated synthesis builds a canonical EvidencePacket;
- valid packet/stages resume idempotently while stale packet stage pointers are
  invalidated/reset rather than reused;
- successful curated synthesis produces the authorities required by the
  existing completion gate but does not itself finish the run;
- one fresh passage cannot globally satisfy a different freshness coverage
  item;
- satisfied terminal admission is rejected transactionally if any exact
  bounded temporal obligation lacks qualifying evidence, leaves the run
  nonterminal on rejection, and succeeds when exact item-bound evidence
  qualifies;
- Ruff, Ruff format, Pyrefly 1.1.1, focused pytest, broader PostgreSQL/Qdrant
  integration tests, and `git diff --check` all pass against the exact branch
  head.
