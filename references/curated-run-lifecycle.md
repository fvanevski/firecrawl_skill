# Curated direct-acquisition lifecycle

Issue #212 adds an explicit run-mode contract for direct Firecrawl work while
preserving PostgreSQL as the authority for lifecycle, invocation provenance,
asset promotion, exact completion membership, and completion. `BLOB_ROOT`
remains the immutable payload store. Qdrant remains a rebuildable projection,
and Valkey remains optional transient coordination.

## Run modes

`frun start` accepts the additive `--run-mode autonomous|curated` option. The
existing `--mode agent_led|autonomous_local|deterministic_debug` option still
selects execution policy and has not changed meaning.

The declared run mode is stored in `research_runs.metadata.run_mode`. New
`frun` runs always persist either `autonomous` or `curated`. Historical rows
without the field are reported as `legacy_unspecified`; no migration infers or
backfills historical mode, lifecycle state, or provenance.

## Canonical curated sequence

A direct wrapper invocation is legal only while the run is in `acquiring`.
Neither `fsearch` nor `fscrape` moves lifecycle state when it begins or
completes. The run must be prepared explicitly before either wrapper executes.

```text
frun start "objective" --run-mode curated
frun prepare <fr_id>
fsearch ... --research-run-id <fr_id>
fscrape ... --research-run-id <fr_id>
frun retain <fr_id> <promotion_subject_id>
frun reject <fr_id> <promotion_subject_id> --reason "..."
frun seal-acquisition <fr_id>
frun resume <fr_id>
frun finish <fr_id> --outcome satisfied
```

`frun prepare` is the explicit, idempotent transition from `created`,
`planning`, or `corpus_review` to `acquiring`. An invalid direct call fails
before provider execution and reports the current state together with the
`frun prepare` remediation.

Regression and integration fixtures follow the same contract: creating a run
does not prepare it. Tests that exercise a real direct wrapper invoke the
explicit preparation boundary rather than relying on an implicit wrapper
transition.

## Authoritative direct-invocation start

The production `fsearch` builder uses `DirectInvocationService`. Its start
transaction locks the run row with `FOR SHARE`, verifies that the locked state
is exactly `acquiring`, and records the exact lifecycle state and revision in:

- `research_invocations.lifecycle_revision`;
- invocation JSONB metadata; and
- the append-only `invocation_started` event.

The production `fscrape` path owns a specialized direct-scrape transaction. It
locks and revalidates the run before inserting the `direct_scrape` invocation,
then writes the same state and revision to the invocation row, JSONB metadata,
and `direct_scrape_started` event. The transport adapter is constructed only
after that transaction commits.

A lifecycle transition cannot interleave between the locked state observation
and invocation insertion. If the locked state is no longer `acquiring`, no
invocation or start event is committed and no provider executes.

Historical invocations are not rewritten.

## Run-scoped retain and reject

`retain`, `reject`, and `seal-acquisition` are available only for runs declared
`curated`. Retain and reject reuse the staged asset-promotion authority from
issue #211.

The command-supplied run UUID, subject UUID, lifecycle revision, ownership
check, run lock, and subject mutation are validated in the same PostgreSQL
transaction. A subject belonging to another run is rejected even when both
runs happen to have the same lifecycle revision.

## Membership sealing and repair

`seal-acquisition` is the only curated command that advances the run from
`acquiring` through `extracting` to `indexing`. It then promotes retained
subjects through evidence eligibility, seals exact PostgreSQL completion
membership, and binds the chunk set used by indexing. It never discovers
candidates, invokes an autonomous planner, or performs smart expansion.

The lifecycle transitions and membership operations are separately durable so
that incremental promotion remains restartable. Therefore the command has an
explicit repair contract for interruption after the run reaches `indexing` but
before an active membership seal exists:

1. `frun resume <fr_id>` reports `frun seal-acquisition <fr_id>` and does not
   start checkpoint processing.
2. `frun finish` fails closed and reports that active completion membership is
   missing.
3. Re-running `frun seal-acquisition <fr_id>` resumes promotion and sealing at
   the existing lifecycle revision without repeating the `extracting` or
   `indexing` transitions.
4. Only after an active seal exists does `frun resume` invoke the bounded index
   checkpoint workflow.

Repeating a completed seal returns the existing authoritative seal. A changed
completion-critical set, stale lifecycle revision, missing compatible chunks,
or historical unstructured asset remains a typed failure rather than being
silently inferred or accepted.

## Resume and finish

For a curated run, `frun resume` is mode- and seal-aware:

- before acquisition, it reports `frun prepare` as the next action;
- during acquisition or extraction, it reports `frun seal-acquisition`;
- in `indexing` without an active seal, it reports `frun seal-acquisition`;
- in `indexing` with an active seal, it resumes the bounded checkpoint workflow;
- after indexing, it reports the explicit finish command; and
- after terminal completion, it is an observable no-op.

`frun finish` uses the existing checkpoint-guarded finish boundary. It never
invokes autonomous search expansion. Repeating finish after a terminal result
returns the same authoritative status without creating new transitions.

## Schema and compatibility impact

No DDL migration is required. Run mode and direct-invocation lifecycle state
are additive keys in existing PostgreSQL JSONB metadata, while the existing
non-null `research_invocations.lifecycle_revision` column remains the numeric
revision authority. Compatibility reads return `legacy_unspecified` or a
missing lifecycle-state key for historical records instead of fabricating
history.

## Required validation

The issue-specific gate exercises all of the following on Python 3.11 and 3.12:

- real `build_fsearch_service()` and `build_fscrape_service()` entry points;
- exact invocation-row and start-event lifecycle provenance;
- rejection of an ineligible state under the insertion lock;
- a real PostgreSQL concurrency test proving lifecycle transitions wait for the
  invocation start transaction;
- cross-run retain/reject ownership rejection;
- interruption after indexing transition and seal-aware repair;
- idempotent seal, resume, finish, and terminal retry behavior; and
- the complete four-asset curated lifecycle without smart expansion.
