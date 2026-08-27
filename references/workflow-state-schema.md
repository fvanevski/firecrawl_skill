# Workflow State Schema

PostgreSQL is the sole authority for research workflow state, durable decisions, acquisition provenance, evidence membership, and corpus identities. Under Target A, immutable provider payload bytes remain in `BLOB_ROOT`. Qdrant and Valkey cannot advance workflow state or establish evidence/currentness authority.

This is a current canonical reference. Normal agent behavior is defined by this document together with `SKILL.md` and `authoritative-workflows.md`. Historical issue/remediation documents do not override it.

## Authority and invariants

- `research_runs.state` is the authoritative lifecycle state.
- `research_runs.lifecycle_revision` is monotonic and used for compare-and-swap.
- `research_run_transitions` and `research_events` are append-only.
- ResearchSpec, SearchPlan, controller policy, operator actions, evidence, terminal decisions, and completion provenance are persisted authority.
- Public normal-agent identities are `fr_<uuid>` for research runs and `oa_<uuid>` for durable human actions.
- Internal PostgreSQL run UUIDs, lifecycle revisions, spec/plan UUIDs, budget-check IDs, provider-recency parameters, and membership fingerprints remain inside the application boundary.
- Idempotency keys reject conflicting reuse.
- Terminal partial is not objective-satisfied.
- Qdrant is a rebuildable projection; Valkey is transient coordination.
- Failed authoritative preflight occurs before provider construction or network invocation.

## Canonical control plane

For ordinary autonomous or retained-first research, use the deterministic controller:

```bash
scripts/fresearch run '<objective>'
scripts/fresearch continue 'fr_<uuid>'
scripts/fresearch status 'fr_<uuid>'
scripts/fresearch result 'fr_<uuid>'
```

The controller owns lifecycle progression. The outer agent does not select or formulate low-level lifecycle commands such as `frun prepare`, `frun seal-acquisition`, `frun resume`, provider search/scrape continuation, or synthesis progression.

`scripts/fsearch_smart` is only a deprecated exact compatibility delegate to `scripts/fresearch run`. It owns no planning, lifecycle, budget, environment, or recovery policy. Retired `fsearch_smart` options such as `--dry-run`, `--spec-skeleton`, `--research-run-id`, and checkpoint-recovery grammar are not part of the current public smart surface.

### Typed continuation/result authority

`workflow-directive-v2` and `research-result-v3` are machine contracts. Free-text diagnostics are explanatory only.

- `continue_automatic` → continue the same `fr_<uuid>` with `fresearch continue`.
- `operator_action_required` → inspect/resolve the returned `oa_<uuid>` human action.
- `terminal_completed` → objective satisfied and final delivery authority available.
- `terminal_partial` → terminal but objective not satisfied; preserve limitations.
- `blocked` / `failed` → fail closed; do not invent recovery choreography.
- `cancelled` → terminal cancellation.

A completed lifecycle whose exact canonical handoff cannot be verified is not returned as a usable completed result; delivery fails closed even though the persisted lifecycle fact remains visible.

## Human-decision boundary

Only genuine human decisions cross the controller boundary:

```bash
scripts/fresearch action 'oa_<uuid>'
scripts/fresearch approve 'oa_<uuid>' --reason '<reason>' --authorized-by '<human>'
scripts/fresearch curate 'oa_<uuid>' --retain '<subject-uuid>' --reject-rest --reason '<reason>' --authorized-by '<human>'
scripts/fresearch fork 'oa_<uuid>' '<revised objective>' --reason '<reason>' --authorized-by '<human>'
```

Soft candidate-budget exceptions require explicit durable authorization; hard violations are not approvable. Curated mode requires one bounded authoritative selection. A material scope change creates a child run with explicit lineage rather than mutating the parent run's legal evidence meaning.

## State machine

The persisted lifecycle remains:

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

The state name `synthesizing` is an internal lifecycle boundary, not proof that prose generation occurred. In default `host_handoff` delivery, a validated EvidencePacket crosses that boundary without redundant full-prose model synthesis and proceeds to validation/completion under host-handoff provenance. Explicit `self_synthesized` delivery retains the full internal synthesis path.

## Retained-first progression

Normal controller order is:

```text
raw objective
→ semantic ResearchSpec proposal
→ deterministic validation/materialization
→ corpus_review
→ authoritative retained-evidence evaluation
→ bounded acquisition only when retained evidence is insufficient
→ exact evidence/completion authority
→ bounded final handoff/result
```

Temporal sufficiency comes from ResearchSpec obligations plus canonical publication/update provenance. Retrieval time, provider recency, ranking, Qdrant state, or agent judgment cannot establish freshness.

## Specialist acquisition boundary

`frun`, `fsearch`, `fscrape`, `finspect`, `research-db`, and `candidate-budget` remain supported specialist/operator/debug/integration surfaces. They do not constitute the normal outer-agent workflow language.

For explicitly controlled direct acquisition, the specialist sequence requires an acquisition-eligible run before provider execution. In particular, `fsearch` and `fscrape` do not implicitly prepare lifecycle state. Use current `frun`/operator documentation and parser contracts rather than inferring progression from old examples.

Every supported direct provider invocation:

1. validates arguments and schema contracts;
2. validates database/schema/blob/run eligibility;
3. records exact invocation/lifecycle authority;
4. only then invokes the provider;
5. commits retained provider/corpus/job provenance;
6. returns bounded stable authoritative identities.

A stale lifecycle revision requires a fresh authoritative status read. Repeating identical input with the same idempotency key may replay the committed operation; a new key denotes a genuinely new operation.

## Completion and delivery

Normal terminal delivery uses:

```bash
scripts/fresearch result 'fr_<uuid>'
```

Default `host_handoff` completion is bound to exact sealed source membership, the exact validated EvidencePacket revision/hash, persisted claim/evidence links, coverage authority, and a deterministic handoff-authority digest. It does not fabricate placeholder synthesis artifacts merely to satisfy the older full-report path.

Explicit `self_synthesized` mode retains the pre-existing semantic draft/citation/validation provenance chain. Delivery mode does not relax coverage, temporal, membership, evidence, or terminal-decision gates.

The public `research-handoff-v1` surface sanitizes application-generated internal identities. It exposes the public run ID, semantic objective/spec authority, bounded citation-ready aliases, coverage/temporal qualification, limitations, unresolved-item summaries, and exact evidence-delivery hashes needed to bind the handoff.

## RTK argv contract

`rtk proxy` forwards an argv vector; it does **not** shell-split `argv[0]`. The first argument after `rtk proxy` must be one executable path/name.

```bash
rtk proxy "<skill-root>/scripts/fresearch" run "<objective>"
rtk proxy "<skill-root>/scripts/fresearch" continue "fr_<uuid>"
rtk proxy python3 "<skill-root>/scripts/drain_index_jobs.py" --batch-size 64
```

**Wrong:** `rtk proxy "python3 <skill-root>/scripts/drain_index_jobs.py" --batch-size 64`.

RTK is an efficiency wrapper only. Its absence never authorizes provider/API bypass.

## Repair and inspection

Normal controller repair starts from public typed state:

```bash
scripts/fresearch status 'fr_<uuid>'
scripts/fresearch result 'fr_<uuid>'
```

Use specialist bounded reads when deeper diagnosis is necessary:

```bash
scripts/research-db run-status 'fr_<uuid>'
scripts/finspect operations --run 'fr_<uuid>'
scripts/finspect invocations --run 'fr_<uuid>'
scripts/finspect attempts --run 'fr_<uuid>'
scripts/research-db verify-blobs
scripts/research-db doctor
```

Never edit ledgers, synthesize state from exports, or let Qdrant/Valkey/local files become workflow authority. Explicit low-level reopen remains a specialist operation for intentional same-lifecycle work; it is not the normal response to a completed controller run. Material scope change uses the durable controller fork/child-run boundary.

The clean schema head is `0038_postgres_authority`. See `migration-guide.md` for the exact legacy-tree import boundary.
